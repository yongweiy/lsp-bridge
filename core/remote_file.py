#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2022 Andy Stewart
#
# Author:     Andy Stewart <lazycat.manatee@gmail.com>
# Maintainer: Andy Stewart <lazycat.manatee@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import threading
import os
import glob
import json
import socket
import traceback
import time
from core.utils import *
from contextlib import contextmanager


class SendMessageException(Exception):
    pass


class ContainerConnectionException(Exception):
    pass


import subprocess
import io


class SubprocessSSHChannel:
    """A channel-like wrapper around an ssh -W subprocess."""

    def __init__(self, proc):
        self._proc = proc
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        self._stderr = proc.stderr

    def sendall(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._stdin.write(data)
        self._stdin.flush()

    def makefile(self, mode="r"):
        return io.TextIOWrapper(self._stdout, encoding="utf-8")

    def close(self):
        try:
            self._proc.terminate()
        except OSError:
            pass
        rc = self._proc.poll()
        if self._stderr:
            try:
                err = self._stderr.read()
                if err:
                    print(f"ssh -W stderr (rc={rc}): {err.decode(errors='replace').strip()}")
            except Exception:
                pass


class SubprocessSSHClient:
    """Minimal SSHClient replacement using subprocess ssh.

    Provides exec_command() and open_channel() by spawning ssh processes.
    Used as a fallback when paramiko cannot authenticate (e.g. cert-based
    agents like Midway).

    Uses the SSH config alias directly so OpenSSH picks up all config
    (ProxyCommand, IdentityAgent, etc.) natively.
    """

    def __init__(self, ssh_conf):
        # Prefer the SSH config alias so `ssh <alias>` picks up all settings
        self._target = ssh_conf.get('_alias', ssh_conf['hostname'])
        self._conf = ssh_conf
        # Verify connectivity with a quick command
        proc = subprocess.run(
            self._ssh_base() + ['true'],
            timeout=30, capture_output=True
        )
        if proc.returncode != 0:
            import paramiko
            raise paramiko.AuthenticationException(
                f"subprocess ssh failed: {proc.stderr.decode(errors='replace')}"
            )

    def _ssh_base(self):
        return ['ssh', '-o', 'BatchMode=yes', '-o', 'PermitLocalCommand=no', self._target]

    def exec_command(self, command):
        proc = subprocess.Popen(
            self._ssh_base() + [command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Return raw byte streams like paramiko does
        return None, proc.stdout, proc.stderr

    def get_transport(self):
        return self

    def open_channel(self, kind, dest_addr, src_addr):
        host, port = dest_addr
        proc = subprocess.Popen(
            self._ssh_base() + ['-W', f'{host}:{port}'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return SubprocessSSHChannel(proc)

    def close(self):
        pass


class RemoteFileClient(threading.Thread):
    remote_password_dict = {}

    def __init__(self, ssh_conf, server_port, callback):
        threading.Thread.__init__(self)

        # Init.
        self.ssh_conf = ssh_conf
        self.ssh_host = ssh_conf['hostname']
        self.ssh_user = ssh_conf.get('user', "root")
        self.ssh_port = ssh_conf.get('port', 22)
        self.server_port = server_port
        self.callback = callback
        [self.remote_python_command, self.remote_python_file, self.remote_log] = get_emacs_vars(["lsp-bridge-remote-python-command", "lsp-bridge-remote-python-file", "lsp-bridge-remote-log"])

        [self.user_ssh_private_key,
         self.user_ssh_agent] = get_emacs_vars(["lsp-bridge-user-ssh-private-key",
                                                "lsp-bridge-user-ssh-agent"])

        self.ssh = self.connect_ssh(
            ssh_conf.get('gssapiauthentication', 'no') in ('yes'),
            ssh_conf.get('proxycommand', None)
        )
        # after successful login, don't create a channel yet
        # caller can use client ssh to execute command on remote server
        # ande then call create_channel() to create the channel
        self.chan = None

        # Death notification: fires at most once when the connection is lost.
        self._dead = threading.Event()
        self.on_dead = None

    def close(self):
        """Close the SSH channel and transport, releasing file descriptors."""
        try:
            if self.chan is not None:
                self.chan.close()
        except Exception:
            pass
        try:
            if self.ssh is not None:
                self.ssh.close()
        except Exception:
            pass

    def _notify_dead(self):
        """Signal that this connection is dead.  Fires on_dead at most once."""
        if self._dead.is_set():
            return
        self._dead.set()
        callback = self.on_dead
        if callback is not None:
            try:
                callback(self)
            except Exception:
                logger.error(traceback.format_exc())

    def ssh_private_key(self):
        """Retrieves the path to the SSH private key file.

        Priority:
        1. Emacs variable `lsp-bridge-user-ssh-private-key`
        2. `identityfile` from the parsed SSH config
        3. First .pub file found in ~/.ssh/ (original fallback)
        """
        if self.user_ssh_private_key:
            return os.path.expanduser(self.user_ssh_private_key)

        # Use identityfile from SSH config if available
        identity_files = self.ssh_conf.get('identityfile', [])
        if identity_files:
            # paramiko returns identityfile as a list; use the first entry
            return os.path.expanduser(identity_files[0])

        ssh_dir = os.path.expanduser("~/.ssh")
        pub_keys = glob.glob(os.path.join(ssh_dir, "*.pub"))
        if pub_keys:
            return pub_keys[0][: -len(".pub")]

        return None

    def connect_ssh(self, use_gssapi, proxy_command):
        """Connect to remote ssh_host

        :raises: :class:`paramiko.AuthenticationException`: if all authentication method failed
        """
        import paramiko

        # Workaround for paramiko bug: AgentKey missing public_blob attribute
        # causes AttributeError during agent auth when inner_key is None.
        # https://github.com/paramiko/paramiko/issues/2462
        from paramiko.agent import AgentKey
        if not hasattr(AgentKey, '_lsp_bridge_patched'):
            _orig_getattr = AgentKey.__getattr__ if hasattr(AgentKey, '__getattr__') else None
            def _safe_getattr(self_key, name):
                if name == 'public_blob':
                    return None
                if _orig_getattr:
                    return _orig_getattr(self_key, name)
                raise AttributeError(name)
            AgentKey.__getattr__ = _safe_getattr
            AgentKey._lsp_bridge_patched = True

        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        proxy = None
        if proxy_command:
            proxy = paramiko.ProxyCommand(proxy_command)

        # When IdentityAgent is configured (e.g. Midway's mcs-agent.sock),
        # paramiko can't use cert-based agent keys for signing.  If a
        # ProxyCommand like wssh handles auth transparently, just disable
        # paramiko's agent auth to avoid the broken code path.
        identity_agent = self.ssh_conf.get('identityagent', None)
        skip_agent = bool(identity_agent and proxy_command)

        try:
            if use_gssapi:
                ssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, gss_auth=True, gss_kex=True, sock=proxy)
            else:
                # Login server with ssh private key.
                # Don't specify key_filename with ssh-agent (allow_agent is default to True)
                ssh_private_key = self.ssh_private_key() if not self.user_ssh_agent else None
                # look_for_keys defaults to True
                # when user specify the SSH private key path
                # disable searching for discoverable private key files in ~/.ssh/
                look_for_keys = not self.user_ssh_private_key
                ssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, key_filename=ssh_private_key, look_for_keys=look_for_keys, allow_agent=not skip_agent, sock=proxy)
        except:
            print(traceback.format_exc())

            # Try subprocess ssh as fallback
            try:
                print("Paramiko auth failed, trying subprocess ssh fallback...")
                ssh = SubprocessSSHClient(self.ssh_conf)
                print("Subprocess ssh fallback connected successfully")
            except Exception as subprocess_err:
                print(traceback.format_exc())

                # If the failure is Midway/WSSH related, prompting for a
                # password is pointless: cert-only auth, no password exists.
                # Surface a clear message and bail instead of asking the user.
                err_str = str(subprocess_err)
                if "WSSH" in err_str or "Midway" in err_str or "mwinit" in err_str:
                    message_emacs(
                        f"Midway/WSSH auth failed for {self.ssh_host}; "
                        "run `mwinit` and retry."
                    )
                    raise paramiko.AuthenticationException(
                        "Midway authentication required (run mwinit)"
                    ) from subprocess_err

                # Last resort: try paramiko with password
                try:
                    password = RemoteFileClient.remote_password_dict[self.ssh_host] if self.ssh_host in RemoteFileClient.remote_password_dict else get_ssh_password(self.ssh_user, self.ssh_host, self.ssh_port)

                    ssh = paramiko.SSHClient()
                    ssh.load_system_host_keys()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(self.ssh_host, port=self.ssh_port, username=self.ssh_user, password=password, sock=proxy)

                    RemoteFileClient.remote_password_dict[self.ssh_host] = password
                except:
                    print(traceback.format_exc())
                    raise paramiko.AuthenticationException()

        return ssh

    def create_channel(self):
        """Create channel to lsp-bridge process running in server

        :raises: :class:`paramiko.ChannelException`: if server lsp-bridge process doesn't exisit
        """
        # The destination of direct-tcpip is resolved by the *remote* sshd, not us.
        # Using the host's FQDN here can fail if the FQDN resolves to a link-local
        # address on the remote side (e.g. EC2 dev desktops where the FQDN
        # resolves to fe80::...). 127.0.0.1 always works because the lsp-bridge
        # remote process listens on 0.0.0.0.
        self.chan = self.ssh.get_transport().open_channel(
            "direct-tcpip", ("127.0.0.1", self.server_port), ("0.0.0.0", 0)
        )
        if self.chan:
            [self.remote_heartbeat_interval] = get_emacs_vars(["lsp-bridge-remote-heartbeat-interval"])
            if self.remote_heartbeat_interval and self.remote_heartbeat_interval != 0:
                threading.Thread(target=self.heartbeat).start()

    def heartbeat(self):
        try:
            while True:
                self.chan.sendall("ping\n".encode("utf-8"))
                log_time_debug(f"Ping server: {self.ssh_host}, port: {self.server_port}")
                time.sleep(self.remote_heartbeat_interval)
        except Exception as e:
            logger.exception(e)
            self._notify_dead()

    def send_message(self, message):
        """Send message via the channel

        :raises: :class:`SendMessageException`: if channel is invalid
        """
        try:
            data = json.dumps(message)
            self.chan.sendall(f"{data}\n".encode("utf-8"))
        except Exception as e:
            raise SendMessageException() from e
        else:
            log_time_debug(f"Sended to server {self.ssh_host} port {self.server_port}: {message}")

    def run(self):
        try:
            chan_file = self.chan.makefile("r")
            while True:
                data = chan_file.readline().strip()
                if not data:
                    print(f"Channel EOF from {self.ssh_host}:{self.server_port}")
                    break

                try:
                    message = parse_json_content(data)
                except Exception:
                    # Skip non-JSON lines (e.g. SSH banners, heartbeat pongs)
                    print(f"Skipping non-JSON data from {self.ssh_host}: {data[:200]}")
                    continue
                log_time_debug(f"Received from server {self.ssh_host} port {self.server_port}: {message}")
                self.callback(message)
        except Exception as e:
            logger.exception(e)
        finally:
            self.chan.close()
            self._notify_dead()

    def start_lsp_bridge_process(self):
        remote_python_command = self.remote_python_command
        remote_python_file = self.remote_python_file
        remote_log = self.remote_log

        # use -l option to bash as a login shell, ensuring that login scripts (like ~/.bash_profile) are read and executed.
        # This is useful for lsp-bridge to use environment settings to correctly find out language server command
        _, stdout, stderr = self.ssh.exec_command(
            f"""
            nohup /bin/bash -l -c '
            pid=$(pgrep -f '\\''lsp_bridge.py$'\\'')
            if [ "$pid" == "" ]; then
                echo -e "Start lsp-bridge process as user $(whoami)" | tee >{remote_log}
                {remote_python_command} {remote_python_file} >>{remote_log} 2>&1 &
                if [ "$?" = "0" ]; then
                    echo -e "Start lsp-bridge successfully" | tee >>{remote_log}
                else
                    echo -e "Start lsp-bridge failed" | tee >>{remote_log}
                fi
            fi'
        """
        )
        print(f"Remote process started at {self.ssh_host}")
        print("stdout:" + stdout.read().decode())
        print("stderr:" + stderr.read().decode())

    def kill_lsp_bridge_process(self):
        remote_log = self.remote_log

        try:
            self.ssh.exec_command(
                f"""
                nohup /bin/bash -l -c '
                pid=$(pgrep -f '\\''lsp_bridge.py$'\\'')
                echo "try kill $pid" | tee >> {remote_log}
                if ! [ "$pid" == "" ]; then
                    echo -e "kill lsp-bridge process as user $(whoami)" | tee >>{remote_log}
                    kill $pid
                    if [ "$?" = "0" ]; then
                        echo -e "Kill lsp-bridge successfully" | tee >>{remote_log}
                    else
                        echo -e "Kill lsp-bridge failed" | tee >>{remote_log}
                    fi
                fi'
            """
            )
        except:
            pass


class DockerFileClient(threading.Thread):
    def __init__(self, container_name, server_port, callback):
        threading.Thread.__init__(self)
        self.container_name = container_name
        self.server_port = server_port
        self.callback = callback
        self.sock = None
        self.lock = threading.Lock()  # For thread-safe socket access

        self.connect()

    def connect(self):
        """Connect to docker container

        :raises: :class:`ContainerConnectionException`: if failed to connect
        """
        with self.lock:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # connect to local host
                self.sock.connect(("127.0.0.1", self.server_port))
            except Exception as e:
                raise ContainerConnectionException(e)

    def send_message(self, message):
        """Send message via socket

        :raises: :class:`SendMessageException`: if socket is invalid
        """
        try:
            if self.sock.fileno == -1:
                # container might be restarted, try to reconnect
                time.sleep(1)
                self.connect()

            data = json.dumps(message)
            self.sock.sendall(f"{data}\n".encode("utf-8"))

        except Exception as e:
            logger.exception(e)
            raise e
        else:
            log_time_debug(f"Sended to server {self.container_name} port {self.server_port}: {message}")

    def run(self):
        """Continuously listen for incoming data from the server."""
        try:
            sock_file = self.sock.makefile("r")
            while True:
                message = sock_file.readline().strip()
                if not message:
                    break

                message = parse_json_content(message)
                message["host"] = self.container_name
                log_time_debug(f"Received from server {self.container_name} port {self.server_port}: {message}")
                self.callback(message)

        except socket.error as e:
            raise SendMessageException() from e
        finally:
            self.sock.close()

class RemoteFileServer:
    def __init__(self, host, port):
        import socket

        # Init.
        self.host = host
        self.port = port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(5)

        # Build event loop.
        self.event_loop = threading.Thread(target=self.event_dispatcher)
        self.event_loop.start()

        self.client_socket = None
        self.client_address = None

    def event_dispatcher(self):
        try:
            while True:
                self.client_socket, self.client_address = self.server.accept()

                threading.Thread(target=self.handle_client).start()
        except Exception as e:
            logger.exception(e)

    def handle_client(self):
        # Capture socket/address locally so cleanup doesn't affect a newer
        # connection that may have overwritten self.client_socket.
        sock = self.client_socket
        addr = self.client_address
        try:
            client_file = sock.makefile('r')
            while True:
                data = client_file.readline().strip()
                if not data:
                    break
                elif data == "ping":
                    log_time_debug(f"Server port {self.port} received ping from client {addr}")
                    continue

                try:
                    message = parse_json_content(data)
                except Exception:
                    logger.error(f"Server port {self.port} failed to parse from client {addr}: {data!r}")
                    continue
                log_time_debug(f"Server port {self.port} received message from client {addr}: {message}")
                resp = self.handle_message(message)
                if resp:
                    sock.send(f"{resp}\n".encode("utf-8"))

            client_file.close()
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            log_time(f"Server port {self.port} socket close for client {addr}")
            # Only clear instance variables if they still point to this socket.
            if self.client_socket is sock:
                self.client_socket = None
                self.client_address = None
        except Exception as e:
            logger.exception(e)

    def handle_message(self, message):
        return

    def send_message(self, message):
        try:
            if self.client_socket:
                if self.client_address:
                    message["host"] = self.client_address[0]

                data = json.dumps(message)
                self.client_socket.send(f"{data}\n".encode("utf-8"))
        except Exception as e:
            logger.exception(e)
            raise SendMessageException() from e
        else:
            log_time_debug(f"Server port {self.port} sended to client {self.client_address}: {message}")


class FileSyncServer(RemoteFileServer):
    def __init__(self, host, port):
        super().__init__(host, port)
        self.file_dict = {}
        self.file_locks = {}

    def handle_message(self, message):
        command = message["command"]

        if command == "open_file":
            return self.handle_open_file(message)
        elif command == "save_file":
            return self.handle_save_file(message)
        elif command == "close_file":
            return self.handle_close_file(message)
        elif command == "change_file":
            return self.handle_change_file(message)
        elif command == "remote_sync":
            return self.handle_remote_sync(message)
        elif command == "update_file":
            return self.handle_update_file(message)

    @contextmanager
    def file_access_lock(self, path):
        path = os.path.abspath(os.path.expanduser(path))

        #  use lock to prevent
        #    - utils.get_buffer_content
        #    - utils.get_file_content_from_file_server
        # from reading file content before handle_open_file finishes.
        lock = self.file_locks.setdefault(path, threading.Lock())
        try:
            lock.acquire()
            yield
        finally:
            lock.release()

    def get_file_content(self, path):
        if path in self.file_dict:
            return self.file_dict[path]
        else:
            # wait for the lock if only path haven't been read
            with self.file_access_lock(path):
                return self.file_dict.get(path, "")

    def handle_update_file(self, message):
        path = message["path"]
        self.file_dict[path] = message["content"]

    def handle_remote_sync(self, message):
        remote_info = message["remote_connection_info"]
        set_remote_connection_info(remote_info)

    def handle_open_file(self, message):
        path = message["path"]
        response = {**message, "path": path}

        if os.path.exists(path):
            with self.file_access_lock(path):
                with open(path) as f:
                    content = f.read()
                    response.update({
                        "path": path,
                        "content": content
                    })
                    self.file_dict[path] = content
        else:
            response.update({
                "path": path,
                "content": "",
                "error": f"Cannot found file {path} on server.",
            })

        return json.dumps(response)

    def handle_change_file(self, message):
        path = message["path"]
        if path not in self.file_dict:
            with open(path) as f:
                self.file_dict[path] = f.read()

        self.file_dict[path] = rebuild_content_from_diff(self.file_dict[path], message["args"][0], message["args"][1], message["args"][3])

    def handle_save_file(self, message):
        path = message["path"]

        if path in self.file_dict:
            with open(path, 'w') as file:
                file.write(self.file_dict[path])

    def handle_close_file(self, message):
        path = message["path"]

        if path in self.file_dict:
            del self.file_dict[path]

    def close_all_files(self):
        self.file_dict.clear()
        self.file_locks.clear()


class FileElispServer(RemoteFileServer):
    def __init__(self, host, port, lsp_bridge):
        self.lsp_bridge = lsp_bridge
        self.rpcs = {}
        super().__init__(host, port)

    def handle_client(self):
        # remote server lsp-bridge process use this cient_socket to call elisp function from local Emacs.
        log_time(f"Client connect from {self.client_address[0]}:{self.client_address[1]}")

        for rpc in self.rpcs.values():
            rpc["result"] = None
            rpc["completion"].set()

        self.lsp_bridge.init_search_backends_complete_event.clear()

        threading.Thread(target=super().handle_client).start()

        try:
            self.lsp_bridge.init_search_backends()
            log_time("init_search_backends finish")
        except Exception:
            logger.exception("init_search_backends failed")
        finally:
            self.lsp_bridge.init_search_backends_complete_event.set()

    def handle_message(self, message):
        if message == "Connect":
            log_time("Drop 'say hello' message from local Emacs.")
            return
        else:
            ts = message["timestamp"]
            # A response may arrive for an RPC we no longer track: on a
            # reconnect the waiting `call_remote_rpc` is woken with a None
            # result and deletes its entry, after which a late response from
            # the previous connection can still land here.  Ignore it instead
            # of raising KeyError, which would crash the receive loop and break
            # the reconnect handshake (get-project-path / get-multi-lang-server
            # callbacks, and thus LSP server relaunch, depend on it).
            rpc = self.rpcs.get(ts)
            if rpc is None:
                log_time(f"Drop stale/unknown RPC response ts={ts}")
                return
            rpc["result"] = message["result"]
            rpc["completion"].set()

    def call_remote_rpc(self, message):
        ts = time.monotonic_ns()
        cpl = threading.Event()
        try:
            message["timestamp"] = ts
            self.rpcs[ts] = { "msg": message, "completion": cpl }
            self.send_message(message)
        except Exception as e:
            logger.exception(e)
            del self.rpcs[ts]
            return None
        else:
            cpl.wait()
            result = self.rpcs[ts]["result"]
            del self.rpcs[ts]
            return result


class FileCommandServer(RemoteFileServer):
    def __init__(self, host, port, lsp_bridge):
        self.lsp_bridge = lsp_bridge
        super().__init__(host, port)

    def handle_client(self):
        # Record server host when lsp-bridge running in remote server.
        # we wait for init_search_backends to finish execution
        # before start thread to handle remote request
        log_time("wait for init_search_backends to finsih execution")
        self.lsp_bridge.init_search_backends_complete_event.wait()

        super().handle_client()

    def handle_message(self, message):
        if message["command"] == "lsp_request":
            # Call LSP request.
            self.lsp_bridge.event_queue.put({
                "name": "action_func",
                "content": ("_{}".format(message["method"]), [message["path"]] + message["args"])
            })
        elif message["command"] == "func_request":
            # Call lsp-bridge normal function.
            getattr(self.lsp_bridge, message["method"])(*message["args"])


def save_ip_to_file(ip, filename):
    with open(filename, 'r') as f:
        existing_ips = f.read().splitlines()

    if ip in existing_ips:
        return

    existing_ips.append(ip)

    with open(filename, 'w') as f:
        f.write('\n'.join(existing_ips))


def get_container_local_ip(container_name):
    try:
        command = "docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' " + container_name
        result = subprocess.check_output(command, shell=True, text=True).strip()
        return result
    except subprocess.CalledProcessError:
        message_emacs(f"get_container_local_ip error: {traceback.format_exc()}")
        return None


def save_ip(ip):
    user_emacs_dir = get_emacs_func_result("get-user-emacs-directory")
    ip_file = os.path.join(user_emacs_dir, "lsp-bridge", "remote_file", "ip.txt")
    touch(ip_file)

    save_ip_to_file(ip, ip_file)
