#!/usr/bin/env python

# Uzbl tabbing wrapper using a fifo socket interface
# Copyright (c) 2009, Tom Adams <tom@holizz.com>
# Copyright (c) 2009, Dieter Plaetinck <dieter AT plaetinck.be>
# Copyright (c) 2009, Mason Larobina <mason.larobina@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# For configuration and cookie daemon usage examples check out the the
# cookie daemon wiki page at http://www.uzbl.org/wiki/cookie_daemon.py
#
# Issues:
#  - There is no easy way of stopping a running daemon.
#
# Todo list:
#  - Use a pid file to make stopping a running daemon easy.
#  - add {start|stop|restart} command line arguments to make the cookie_daemon
#    functionally similar to the daemons found in /etc/init.d/ (in gentoo)
#    or /etc/rc.d/ (in arch).


import cookielib
import os
import sys
import urllib2
import select
import socket
import time
import atexit
from traceback import print_exc
from signal import signal, SIGTERM
from optparse import OptionParser

try:
    import cStringIO as StringIO

except ImportError:
    import StringIO


# ============================================================================
# ::: Default configuration section ::::::::::::::::::::::::::::::::::::::::::
# ============================================================================


# Location of the uzbl cache directory.
if 'XDG_CACHE_HOME' in os.environ.keys() and os.environ['XDG_CACHE_HOME']:
    cache_dir = os.path.join(os.environ['XDG_CACHE_HOME'], 'uzbl/')

else:
    cache_dir = os.path.join(os.environ['HOME'], '.cache/uzbl/')

# Location of the uzbl data directory.
if 'XDG_DATA_HOME' in os.environ.keys() and os.environ['XDG_DATA_HOME']:
    data_dir = os.path.join(os.environ['XDG_DATA_HOME'], 'uzbl/')

else:
    data_dir = os.path.join(os.environ['HOME'], '.local/share/uzbl/')

# Default config
config = {

  # Default cookie jar and daemon socket locations.
  'cookie_socket': os.path.join(cache_dir, 'cookie_daemon_socket'),
  'cookie_jar': os.path.join(data_dir, 'cookies.txt'),

  # Time out after x seconds of inactivity (set to 0 for never time out).
  # Set to 0 by default until talk_to_socket is doing the spawning.
  'daemon_timeout': 0,

  # Tell process to daemonise
  'daemon_mode': True,

  # Set true to print helpful debugging messages to the terminal.
  'verbose': False,

} # End of config dictionary.


# ============================================================================
# ::: End of configuration section :::::::::::::::::::::::::::::::::::::::::::
# ============================================================================


_scriptname = os.path.basename(sys.argv[0])
def echo(msg):
    if config['verbose']:
        print "%s: %s" % (_scriptname, msg)


def mkbasedir(filepath):
    '''Create base directory of filepath if it doesn't exist.'''

    dirname = os.path.dirname(filepath)
    if not os.path.exists(dirname):
        echo("creating dirs: %r" % dirname)
        os.makedirs(dirname)


class CookieMonster:
    '''The uzbl cookie daemon class.'''

    def __init__(self):
        '''Initialise class variables.'''

        self.server_socket = None
        self.jar = None
        self.last_request = time.time()
        self._running = False


    def run(self):
        '''Start the daemon.'''

        # Check if another daemon is running. The reclaim_socket function will
        # exit if another daemon is detected listening on the cookie socket
        # and remove the abandoned socket if there isnt.
        if os.path.exists(config['cookie_socket']):
            self.reclaim_socket()

        # Daemonize process.
        if config['daemon_mode']:
            echo("entering daemon mode.")
            self.daemonize()

        # Register a function to cleanup on exit.
        atexit.register(self.quit)

        # Make SIGTERM act orderly.
        signal(SIGTERM, lambda signum, stack_frame: sys.exit(1))

        # Create cookie jar object from file.
        self.open_cookie_jar()

        # Creating a way to exit nested loops by setting a running flag.
        self._running = True

        while self._running:
            # Create cookie daemon socket.
            self.create_socket()

            try:
                # Enter main listen loop.
                self.listen()

            except KeyboardInterrupt:
                self._running = False
                print

            except socket.error:
                print_exc()

            except:
                # Clean up
                self.del_socket()

                # Raise exception
                raise

            # Always delete the socket before calling create again.
            self.del_socket()


    def reclaim_socket(self):
        '''Check if another process (hopefully a cookie_daemon.py) is listening
        on the cookie daemon socket. If another process is found to be
        listening on the socket exit the daemon immediately and leave the
        socket alone. If the connect fails assume the socket has been abandoned
        and delete it (to be re-created in the create socket function).'''

        cookie_socket = config['cookie_socket']

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            sock.connect(cookie_socket)
            sock.close()

        except socket.error:
            # Failed to connect to cookie_socket so assume it has been
            # abandoned by another cookie daemon process.
            echo("reclaiming abandoned cookie_socket %r." % cookie_socket)
            if os.path.exists(cookie_socket):
                os.remove(cookie_socket)

            return

        echo("detected another process listening on %r." % cookie_socket)
        echo("exiting.")
        # Use os._exit() to avoid tripping the atexit cleanup function.
        os._exit(1)


    def daemonize(function):
        '''Daemonize the process using the Stevens' double-fork magic.'''

        try:
            if os.fork(): os._exit(0)

        except OSError, e:
            sys.stderr.write("fork #1 failed: %s\n" % e)
            sys.exit(1)

        os.chdir('/')
        os.setsid()
        os.umask(0)

        try:
            if os.fork(): os._exit(0)

        except OSError, e:
            sys.stderr.write("fork #2 failed: %s\n" % e)
            sys.exit(1)

        sys.stdout.flush()
        sys.stderr.flush()

        devnull = '/dev/null'
        stdin = file(devnull, 'r')
        stdout = file(devnull, 'a+')
        stderr = file(devnull, 'a+', 0)

        os.dup2(stdin.fileno(), sys.stdin.fileno())
        os.dup2(stdout.fileno(), sys.stdout.fileno())
        os.dup2(stderr.fileno(), sys.stderr.fileno())


    def open_cookie_jar(self):
        '''Open the cookie jar.'''

        cookie_jar = config['cookie_jar']
        if cookie_jar:
            mkbasedir(cookie_jar)

        # Create cookie jar object from file.
        self.jar = cookielib.MozillaCookieJar(cookie_jar)

        if cookie_jar:
            try:
                # Attempt to load cookies from the cookie jar.
                self.jar.load(ignore_discard=True)

                # Ensure restrictive permissions are set on the cookie jar
                # to prevent other users on the system from hi-jacking your
                # authenticated sessions simply by copying your cookie jar.
                os.chmod(cookie_jar, 0600)

            except:
                pass


    def create_socket(self):
        '''Create AF_UNIX socket for interprocess uzbl instance <-> cookie
        daemon communication.'''

        cookie_socket = config['cookie_socket']
        mkbasedir(cookie_socket)

        self.server_socket = socket.socket(socket.AF_UNIX,\
          socket.SOCK_SEQPACKET)

        if os.path.exists(cookie_socket):
            # Accounting for super-rare super-fast racetrack condition.
            self.reclaim_socket()

        self.server_socket.bind(cookie_socket)

        # Set restrictive permissions on the cookie socket to prevent other
        # users on the system from data-mining your cookies.
        os.chmod(cookie_socket, 0600)


    def listen(self):
        '''Listen for incoming cookie PUT and GET requests.'''

        echo("listening on %r" % config['cookie_socket'])

        while self._running:
            # This line tells the socket how many pending incoming connections
            # to enqueue. I haven't had any broken pipe errors so far while
            # using the non-obvious value of 1 under heavy load conditions.
            self.server_socket.listen(1)

            if bool(select.select([self.server_socket],[],[],1)[0]):
                client_socket, _ = self.server_socket.accept()
                self.handle_request(client_socket)
                self.last_request = time.time()
                client_socket.close()

            if config['daemon_timeout']:
                idle = time.time() - self.last_request
                if idle > config['daemon_timeout']:
                    self._running = False


    def handle_request(self, client_socket):
        '''Connection made, now to serve a cookie PUT or GET request.'''

        # Receive cookie request from client.
        data = client_socket.recv(8192)
        if not data: return

        # Cookie argument list in packet is null separated.
        argv = data.split("\0")

        # Catch the EXIT command sent to kill the daemon.
        if len(argv) == 1 and argv[0].strip() == "EXIT":
            self._running = False
            return None

        # Determine whether or not to print cookie data to terminal.
        print_cookie = (config['verbose'] and not config['daemon_mode'])
        if print_cookie: print ' '.join(argv[:4])

        action = argv[0]

        uri = urllib2.urlparse.ParseResult(
          scheme=argv[1],
          netloc=argv[2],
          path=argv[3],
          params='',
          query='',
          fragment='').geturl()

        req = urllib2.Request(uri)

        if action == "GET":
            self.jar.add_cookie_header(req)
            if req.has_header('Cookie'):
                cookie = req.get_header('Cookie')
                client_socket.send(cookie)
                if print_cookie: print cookie

            else:
                client_socket.send("\0")

        elif action == "PUT":
            if len(argv) > 3:
                set_cookie = argv[4]
                if print_cookie: print set_cookie

            else:
                set_cookie = None

            hdr = urllib2.httplib.HTTPMessage(\
              StringIO.StringIO('Set-Cookie: %s' % set_cookie))
            res = urllib2.addinfourl(StringIO.StringIO(), hdr,\
              req.get_full_url())
            self.jar.extract_cookies(res,req)
            if config['cookie_jar']:
                self.jar.save(ignore_discard=True)

        if print_cookie: print


    def quit(self, *args):
        '''Called on exit to make sure all loose ends are tied up.'''

        # Only one loose end so far.
        self.del_socket()

        os._exit(0)


    def del_socket(self):
        '''Remove the cookie_socket file on exit. In a way the cookie_socket
        is the daemons pid file equivalent.'''

        if self.server_socket:
            try:
                self.server_socket.close()

            except:
                pass

        self.server_socket = None

        cookie_socket = config['cookie_socket']
        if os.path.exists(cookie_socket):
            echo("deleting socket %r" % cookie_socket)
            os.remove(cookie_socket)


if __name__ == "__main__":


    parser = OptionParser()
    parser.add_option('-n', '--no-daemon', dest='no_daemon',\
      action='store_true', help="don't daemonise the process.")

    parser.add_option('-v', '--verbose', dest="verbose",\
      action='store_true', help="print verbose output.")

    parser.add_option('-t', '--daemon-timeout', dest='daemon_timeout',\
      action="store", metavar="SECONDS", help="shutdown the daemon after x "\
      "seconds inactivity. WARNING: Do not use this when launching the "\
      "cookie daemon manually.")

    parser.add_option('-s', '--cookie-socket', dest="cookie_socket",\
      metavar="SOCKET", help="manually specify the socket location.")

    parser.add_option('-j', '--cookie-jar', dest='cookie_jar',\
      metavar="FILE", help="manually specify the cookie jar location.")

    parser.add_option('-m', '--memory', dest='memory', action='store_true',
      help="store cookies in memory only - do not write to disk")

    (options, args) = parser.parse_args()

    if options.verbose:
        config['verbose'] = True
        echo("verbose mode on.")

    if options.no_daemon:
        echo("daemon mode off.")
        config['daemon_mode'] = False

    if options.cookie_socket:
        echo("using cookie_socket %r" % options.cookie_socket)
        config['cookie_socket'] = options.cookie_socket

    if options.cookie_jar:
        echo("using cookie_jar %r" % options.cookie_jar)
        config['cookie_jar'] = options.cookie_jar

    if options.memory:
        echo("using memory %r" % options.memory)
        config['cookie_jar'] = None

    if options.daemon_timeout:
        try:
            config['daemon_timeout'] = int(options.daemon_timeout)
            echo("set timeout to %d seconds." % config['daemon_timeout'])

        except ValueError:
            config['verbose'] = True
            echo("fatal error: expected int argument for --daemon-timeout")
            sys.exit(1)

    # Expand $VAR's in config keys that relate to paths.
    for key in ['cookie_socket', 'cookie_jar']:
        if config[key]:
            config[key] = os.path.expandvars(config[key])

    CookieMonster().run()