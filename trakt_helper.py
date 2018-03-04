#!/usr/bin/env python
# encoding: utf-8
#
# TraktForVLC, to link VLC watching to trakt.tv updating
#
# Copyright (C) 2017-2018   Raphaël Beamonte <raphael.beamonte@gmail.com>
#
# This file is part of TraktForVLC.  TraktForVLC is free software:
# you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation,
# version 2.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA
# or see <http://www.gnu.org/licenses/>.

#
# The aim of this file is to provide a helper for any task that cannot
# be performed easily in the lua interface for VLC. The lua interface
# will thus be able to call this tool to perform those tasks and return
# the results
#


import argparse
import datetime
import distutils.spawn
import fuzzywuzzy.fuzz
import glob
import imdbpie
import json
import logging
import math
import os
import platform
import pytz
import re
import requests
import shutil
import stat
import subprocess
import sys
import tvdb_api
try:
    # Python 3
    import xmlrpc.client as xmlrpc
except ImportError:
    # Python 2
    import xmlrpclib as xmlrpc

if platform.system() != 'Windows':
    import pwd

LOGGER = logging.getLogger(__name__)
__version__ = '0.0.0a0.dev0'


##############################################################################
# Get a resource from the same directory as the helper, or from the binary
# if we are currently in the binary.
def get_resource_path(relative_path):
    return os.path.join(
        getattr(
            sys, '_MEIPASS',
            os.path.dirname(os.path.realpath(__file__))
        ),
        relative_path
    )


##############################################################################
# Python equivalent to Linux's 'which' command and Windows' 'where'
# def which(exe, path=None):
    # # If the exe is a path, check if that path exists: if it does, and has the
    # # executable flag, return it, else, return None
    # if os.path.basename(exe) != exe:
        # if os.path.isfile(exe) and os.access(exe, os.X_OK):
            # return exe
        # return None

    # # If no path is provided, use the environment variable
    # if path is None:
        # path = os.getenv('PATH', '')

    # # Split the path according to the pathsep for the operating system
    # path = path.split(os.pathsep)

    # # The extensions that will be used to search for the executable file
    # extensions = ['']

    # if os.name == 'os2':
        # (_, ext) = os.path.splitext(exe)
        # # OS/2 automatically appends .exe to executable files if the name
        # # does not contain any dot
        # if not ext:
            # exe = '{}.exe'.format(exe)
    # elif platform.system() == 'Windows':
        # pathext = os.getenv('PATHEXT', '').lower().split(os.pathsep)
        # (_, ext) = os.path.splitext(exe)
        # if ext.lower() not in pathext:
            # extensions = pathext

    # for ext in extensions:
        # fexe = '{}{}'.format(exe, ext)
        # for p in path:
            # print '[PATH] Searching in {}'.format(p)
            # fpath = os.path.join(p, fexe)
            # if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
                # return fpath


##############################################################################
# Return the path to the VLC executable
def get_vlc():
    if platform.system() == 'Windows':
        environment = ['ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432']
        program_files = set(
            os.environ[e] for e in environment if e in os.environ)
        for p in program_files:
            fpath = os.path.join(p, 'VideoLAN', 'VLC', 'vlc.exe')
            if os.path.isfile(fpath):
                return fpath
    return distutils.spawn.find_executable('vlc')


##############################################################################
# To run a subprocess as user
def run_as_user():
    if platform.system() == 'Windows' or not os.getenv('SUDO_USER'):
        LOGGER.debug('No need to change the user')
        return {}
    
    pw = pwd.getpwnam(os.getenv('SUDO_USER'))
    LOGGER.debug('Providing the parameters to run the command as {}'.format(
        pw.pw_name))
    
    env = os.environ.copy()
    for k in env.keys():
        if k.startswith('SUDO_'):
            del env[k]

    env['HOME'] = pw.pw_dir
    env['LOGNAME'] = env['USER'] = env['USERNAME'] = pw.pw_name

    def demote():
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)

    return {
        'preexec_fn': demote,
        'env': env,
    }


##############################################################################
# To determine the paths to the LUA and Config directories of VLC
def get_os_config(system=None, config=None, lua=None):
    def itmerge(*iterators):
        for iterator in iterators:
            for value in iterator:
                yield value

    if not config or not lua:
        opsys = platform.system()
        if opsys in ['Linux', 'Darwin']:
            if os.getenv('SUDO_USER'):
                home = pwd.getpwnam(os.getenv('SUDO_USER')).pw_dir
            else:
                home = os.path.expanduser('~')
        if opsys == 'Linux':
            if not config:
                config = os.path.join(home, '.config', 'vlc')
            if not lua:
                if system:
                    lua = next(itmerge(
                        glob.iglob('/usr/lib/*/vlc/lua'),
                        glob.iglob('/usr/lib/vlc/lua'),
                    ))
                else:
                    lua = os.path.join(home, '.local', 'share', 'vlc', 'lua')
        elif opsys == 'Darwin':
            if not config:
                config = os.path.join(home, 'Library', 'Application Support',
                                      'org.videolan.vlc')
            if not lua:
                if system:
                    lua = '/Applications/VLC.app/Contents/MacOS/share/lua'
                else:
                    lua = os.path.join(config, 'lua')
        elif opsys == 'Windows':
            if not config:
                config = os.path.join(os.getenv('APPDATA'), 'vlc')
            if not lua:
                if system:
                    lua = os.path.join(
                        os.getenv('PROGRAMFILES'), 'VideoLAN', 'VLC', 'lua')
                else:
                    lua = os.path.join(config, 'lua')
    
    if config and lua:
        return {
            'config': config,
            'lua': lua,
        }

    raise RuntimeError('Unsupported operating system: {}'.format(system))


##############################################################################
# To prompt the user for a yes-no answer
def ask_yes_no(prompt):
    try:
        while 'the feeble-minded user has to provide an answer':
            reply = str(raw_input(
                '{} [y/n] '.format(prompt))).lower().strip()
            if reply in ['y', 'yes', '1']:
                return True
            elif reply in ['n', 'no', '0']:
                return False
    except (KeyboardInterrupt, EOFError):
        print('Installation aborted by signal.')
        return


##############################################################################
# Action to perform when the INSTALL command is used.
def action_install(dry_run, yes, system, vlc_bin, vlc_config, vlc_lua):
    os_config = get_os_config(system, vlc_config, vlc_lua)

    # Try to find the VLC executable if it has not been passed as parameter
    if not vlc_bin:
        LOGGER.info('Searching for VLC binary...')
        vlc_bin = get_vlc()
    # If we still did not find it, cancel the installation as we will not be
    # able to complete it
    if not vlc_bin:
        raise RuntimeError(
            'VLC executable not found: use the --vlc parameter '
            'to specify VLC location')
    else:
        LOGGER.info('VLC binary: {}'.format(vlc_bin))

    # Check that the trakt.luac file can be found
    trakt_lua = get_resource_path('trakt.luac')
    # If it cannot, try to find the trakt.lua file instead
    if not os.path.isfile(trakt_lua):
        trakt_lua = get_resource_path('trakt.lua')
    # If still not found, cancel the installation
    if not os.path.isfile(trakt_lua):
        raise RuntimeError(
            'trakt.luac/trakt.lua file not found, unable to install')

    # Compute the path to the directories we need to use after
    config = os_config['config']
    lua = os_config['lua']
    lua_intf = os.path.join(lua, 'intf')

    # Show install information to the user, and query for approbation if --yes
    # was not used
    print('\n'.join([
        'TraktForVLC will be installed for the following configuration:',
        ' - OS: {}'.format(platform.system()),
        ' - VLC: {}'.format(vlc_bin),
        ' - VLC configuration: {}'.format(config),
        ' - VLC Lua: {}'.format(lua),
        ' - VLC Lua interface: {}'.format(lua_intf),
    ]))
    if not yes:
        yes_no = ask_yes_no('Proceed with installation ?')
        if not yes_no:
            print('Installation aborted by {}.'.format(
                  'signal' if yes_no is None else 'user'))
            return

    # Create all needed directories
    needed_dirs = [lua_intf]

    for d in needed_dirs:
        if not os.path.isdir(d):
            LOGGER.info('Creating directory (and parents): {}'.format(d))
            if not dry_run:
                os.makedirs(d)

    # Copy the trakt helper executable in the Lua directory of VLC
    if getattr(sys, 'frozen', False):
        trakt_helper = sys.executable
        trakt_helper_dest = 'trakt_helper'
        if platform.system() == 'Windows':
            trakt_helper_dest = '{}.exe'.format(trakt_helper_dest)
    else:
        trakt_helper = os.path.realpath(__file__)
        trakt_helper_dest = os.path.basename(__file__)

    LOGGER.info('Copying helper ({}) to {}'.format(
        trakt_helper_dest, lua))
    trakt_helper_path = os.path.join(lua, trakt_helper_dest)
    if not dry_run:
        shutil.copy2(trakt_helper, trakt_helper_path)
    if system and platform.system() != 'Windows':
        LOGGER.info('Setting permissions of {} to 755'.format(
            trakt_helper_path))
        if not dry_run:
            os.chmod(trakt_helper_path,
                     stat.S_IRWXU |
                     stat.S_IRGRP | stat.S_IXGRP |
                     stat.S_IROTH | stat.S_IXOTH)

    # If we put the helper in the lua directory, we need to add the
    # information on how to find it to the trakt_config.json file in the
    # config directory; if there is a helper location in the config but
    # we're installing locally, override by removing that information
    # trakt_config_json = os.path.join(config, 'trakt_config.json')
    # if os.path.isfile(trakt_config_json):
        # with open(trakt_config_json, 'r') as f:
            # trakt_config = json.load(f)
    # else:
        # trakt_config = {}

    # if system or 'helper' in trakt_config:
        # if system:
            # LOGGER.info('Setting the helper path in trakt\'s configuration')
            # trakt_config['helper'] = trakt_helper_path
       
            # if not os.path.isdir(config):
                # LOGGER.info('Creating directory (and parents): {}'.format(
                    # config))
                # if not dry_run:
                    # os.makedirs(config)
        # else:
            # LOGGER.info('Removing the helper path from trakt\'s configuration')
            # del trakt_config['helper']
        
        # if not dry_run:
            # with open(trakt_config_json, 'w') as f:
                # json.dump(trakt_config, f, sort_keys=True,
                          # indent=4, separators=(',', ': '))
    
    # Then copy the trakt.lua file in the Lua interface directory of VLC
    LOGGER.info('Copying {} to {}'.format(
        os.path.basename(trakt_lua), lua_intf))
    trakt_lua_path = os.path.join(lua_intf, os.path.basename(trakt_lua))
    if not dry_run:
        shutil.copy2(trakt_lua, lua_intf)
    if system and platform.system() != 'Windows':
        LOGGER.info('Setting permissions of {} to 644'.format(trakt_lua_path))
        if not dry_run:
            os.chmod(trakt_lua_path,
                     stat.S_IRUSR | stat.S_IWUSR |
                     stat.S_IRGRP |
                     stat.S_IROTH)

    # We then need to start VLC with the trakt interface enabled, and
    # pass the autostart=enable parameter so VLC will be setup
    LOGGER.info('Setting up VLC to automatically use trakt\'s interface')
    configured = False
    if not dry_run:
        command = [
            vlc_bin,
            '-I', 'luaintf',
            '--lua-intf', 'trakt',
            '--lua-config', 'trakt={autostart="enable"}',
        ]
        LOGGER.debug('Running command: {}'.format(
            subprocess.list2cmdline(command)))
        enable = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            **run_as_user()
        )
        output = []
        for line in iter(enable.stdout.readline, b''):
            line = line.strip()
            output.append(line)
            LOGGER.debug(line)
            if line.endswith('[trakt] lua interface: VLC is configured to '
                             'automatically use TraktForVLC'):
                configured = True
        enable.stdout.close()

        if enable.wait() != 0:
            LOGGER.error('Unable to enable VLC '
                         'lua interface:\n{}'.format('\n'.join(output)))
            sys.exit(-1)
    else:
        configured = True

    if configured:
        LOGGER.info('VLC configured')
    else:
        LOGGER.error('Error while configuring VLC')
        sys.exit(-1)

    LOGGER.info('TraktForVLC v{} is now installed. :D'.format(__version__))
    

##############################################################################
# Action to perform when the UNINSTALL command is used.
def action_uninstall(dry_run, yes, system, vlc_bin, vlc_config, vlc_lua):
    os_config = get_os_config(system, vlc_config, vlc_lua)

    # Try to find the VLC executable if it has not been passed as parameter
    if not vlc_bin:
        LOGGER.info('Searching for VLC binary...')
        vlc_bin = get_vlc()
    # If we still did not find it, cancel the installation as we will not be
    # able to complete it
    if not vlc_bin:
        raise RuntimeError(
            'VLC executable not found: use the --vlc parameter '
            'to specify VLC location')
    else:
        LOGGER.info('VLC binary: {}'.format(vlc_bin))

    # Compute the path to the directories we need to use after
    config = os_config['config']
    lua = os_config['lua']
    lua_intf = os.path.join(lua, 'intf')

    # Search for the files to remove
    to_remove = []

    search_files = {
        # The helper
        lua: [
            'trakt_helper',
            'trakt_helper.exe',
            'trakt_helper.py',
        ],
        # The Lua interfaces
        lua_intf: [
            'trakt.lua',
            'trakt.luac',
        ],
    }

    for path, files in sorted(search_files.items()):
        if not files:
            continue

        LOGGER.info('Searching for the files to remove in {}'.format(path))
        for f in files:
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                to_remove.append(fp)

    # Show to the user what's going to be done
    if not to_remove:
        print('No files to be removed. Will still try to disable '
              'trakt\'s lua interface.')
    else:
        to_remove.sort()
        if getattr(sys, 'frozen', False):
            me = sys.executable
        else:
            me = os.path.realpath(__file__)

        # If the executable is in the list of files to remove, push it to
        # the end, we only want it to be removed if we succeeded in removing
        # everything else
        if me in to_remove:
            to_remove.remove(me)
            to_remove.append(me)

        print('Will remove the following files:')
        for f in to_remove:
            if f == me:
                print(' - {} (this is me!!)'.format(f))
            else:
                print(' - {}'.format(f))
        print('And try to disable trakt\'s lua interface.')

    # Prompt before continuing, except if --yes was used
    if not yes:
        yes_no = ask_yes_no('Proceed with uninstallation ?')
        if not yes_no:
            print('Uninstallation aborted by {}.'.format(
                  'signal' if yes_no is None else 'user'))
            return

    # We then need to start VLC with the trakt interface enabled, and
    # pass the autostart=disable parameter so we'll disable the interface
    # from VLC
    LOGGER.info('Setting up VLC not to use trakt\'s interface')
    configured = False
    if not dry_run:
        command = [
            vlc_bin,
            '-I', 'luaintf',
            '--lua-intf', 'trakt',
            '--lua-config', 'trakt={autostart="disable"}',
        ]
        LOGGER.debug('Running command: {}'.format(
            subprocess.list2cmdline(command)))
        disable = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            **run_as_user()
        )
        output = []
        msg_ok = ('[trakt] lua interface: VLC is configured '
                  'not to use TraktForVLC')
        msg_not_exists = ('[trakt] lua interface error: Couldn\'t find '
                          'lua interface script "trakt".')
        for line in iter(disable.stdout.readline, b''):
            line = line.strip()
            output.append(line)
            LOGGER.debug(line)
            if line.endswith(msg_ok) or line.endswith(msg_not_exists):
                configured = True
        disable.stdout.close()

        if disable.wait() != 0 and not configured:
            LOGGER.error('Unable to disable VLC '
                         'lua interface:\n{}'.format('\n'.join(output)))
            sys.exit(-1)
    else:
        configured = True

    if configured:
        LOGGER.info('VLC configured')
    else:
        LOGGER.error('Error while configuring VLC')
        sys.exit(-1)

    # Then we remove the files
    for f in to_remove:
        LOGGER.info('Removing {}'.format(f))
        if not dry_run:
            try:
                os.remove(f)
            except WindowsError:
                if f != me:
                    # If we got a windows error while trying to remove one
                    # of the files that is not the currently running file,
                    # raise the error
                    raise

                LOGGER.info('Cannot remove myself directly, launching a '
                            'subprocess to do so... Bye bye ;(')
                command = subprocess.list2cmdline([
                    # Run that as a shell command
                    'CMD', '/C',
                    # Wait two seconds - So the file descriptor is freed
                    'PING', '127.0.0.1', '-n', '2', '>NUL', '&',
                    # Delete the file
                    'DEL', '/F', '/S', '/Q', f,
                ])
                LOGGER.debug('Running command: {}'.format(command))
                subprocess.Popen(command)
                sys.exit(0)

    LOGGER.info('TraktForVLC is now uninstalled. :(')


##############################################################################
# Action to perform when the REQUESTS command is used.
def action_requests(method, url, headers, data):
    if headers is None:
        headers = {}
    else:
        try:
            headers = {
                k: str(v)
                for k, v in json.loads(headers).items()
            }
        except Exception as e:
            raise RuntimeError(
                'Headers argument could not be parsed as JSON: {}'.format(
                    e.message))

    ##########################################################################
    # Prepare the parameters
    params = {
        'url': url,
        'headers': headers,
    }
    if data is not None:
        try:
            data = json.loads(data)
        except:
            raise RuntimeError('Data argument could not be parsed as JSON')
        params['json'] = data
    
    ##########################################################################
    # Find the request method to use
    req_func = getattr(requests, method.lower(), None)
    if not req_func:
        raise RuntimeError('Function to perform HTTP/HTTPS request for '
                           'method {} not found'.format(method))
    
    ##########################################################################
    # Perform the request
    resp = req_func(**params)

    ##########################################################################
    # Prepare the result dict
    result = {
        'status_code': resp.status_code,
        'reason': resp.reason,
        'url': resp.url,
        'headers': dict(resp.headers),
        'body': resp.text,
        'request': {
            'url': resp.request.url,
            'method': resp.request.method,
            'headers': dict(resp.request.headers),
            'body': resp.request.body or '',
        },
    }
    try:
        result['json'] = resp.json()
    except:
        pass

    ##########################################################################
    # Print the JSON dump of the result
    print(json.dumps(result, sort_keys=True,
                     indent=4, separators=(',', ': ')))


##############################################################################
# Parse a filename to the series/movie
def parse_filename(filename):
    if type(filename) == bytes:
        filename = filename.decode()

    def cleanRegexedName(name):
        name = re.sub("[.](?!.(?:[.]|$))", " ", name)
        name = re.sub("(?<=[^. ]{2})[.]", " ", name)
        name = name.replace("_", " ")
        name = name.strip("- ")
        return name

    found = {}

    # Patterns to parse input series filenames with
    # These patterns come directly from the tvnamer project available
    # on https://github.com/dbr/tvnamer
    series_patterns = [
        # [group] Show - 01-02 [crc]
        '''^\[(?P<group>.+?)\][ ]?
            (?P<seriesname>.*?)[ ]?[-_][ ]?
            (?P<episodenumberstart>\d+)
            ([-_]\d+)*
            [-_](?P<episodenumberend>\d+)
            (?=
              .*
              \[(?P<crc>.+?)\]
            )?
            [^\/]*$''',

        # [group] Show - 01 [crc]
        '''^\[(?P<group>.+?)\][ ]?
            (?P<seriesname>.*)
            [ ]?[-_][ ]?
            (?P<episodenumber>\d+)
            (?=
              .*
              \[(?P<crc>.+?)\]
            )?
            [^\/]*$''',

        # foo s01e23 s01e24 s01e25 *
        '''^((?P<seriesname>.+?)[ \._\-])?
            [Ss](?P<seasonnumber>[0-9]+)
            [\.\- ]?
            [Ee](?P<episodenumberstart>[0-9]+)
            ([\.\- ]+
            [Ss](?P=seasonnumber)
            [\.\- ]?
            [Ee][0-9]+)*
            ([\.\- ]+
            [Ss](?P=seasonnumber)
            [\.\- ]?
            [Ee](?P<episodenumberend>[0-9]+))
            [^\/]*$''',

        # foo.s01e23e24*
        '''^((?P<seriesname>.+?)[ \._\-])?
            [Ss](?P<seasonnumber>[0-9]+)
            [\.\- ]?
            [Ee](?P<episodenumberstart>[0-9]+)
            ([\.\- ]?
            [Ee][0-9]+)*
            [\.\- ]?[Ee](?P<episodenumberend>[0-9]+)
            [^\/]*$''',

        # foo.1x23 1x24 1x25
        '''^((?P<seriesname>.+?)[ \._\-])?
            (?P<seasonnumber>[0-9]+)
            [xX](?P<episodenumberstart>[0-9]+)
            ([ \._\-]+
            (?P=seasonnumber)
            [xX][0-9]+)*
            ([ \._\-]+
            (?P=seasonnumber)
            [xX](?P<episodenumberend>[0-9]+))
            [^\/]*$''',

        # foo.1x23x24*
        '''^((?P<seriesname>.+?)[ \._\-])?
            (?P<seasonnumber>[0-9]+)
            [xX](?P<episodenumberstart>[0-9]+)
            ([xX][0-9]+)*
            [xX](?P<episodenumberend>[0-9]+)
            [^\/]*$''',

        # foo.s01e23-24*
        '''^((?P<seriesname>.+?)[ \._\-])?
            [Ss](?P<seasonnumber>[0-9]+)
            [\.\- ]?
            [Ee](?P<episodenumberstart>[0-9]+)
            (
                 [\-]
                 [Ee]?[0-9]+
            )*
                 [\-]
                 [Ee]?(?P<episodenumberend>[0-9]+)
            [\.\- ]
            [^\/]*$''',

        # foo.1x23-24*
        '''^((?P<seriesname>.+?)[ \._\-])?
            (?P<seasonnumber>[0-9]+)
            [xX](?P<episodenumberstart>[0-9]+)
            (
                 [\-+][0-9]+
            )*
                 [\-+]
                 (?P<episodenumberend>[0-9]+)
            ([\.\-+ ].*
            |
            $)''',

        # foo.[1x09-11]*
        '''^(?P<seriesname>.+?)[ \._\-]
            \[
                ?(?P<seasonnumber>[0-9]+)
            [xX]
                (?P<episodenumberstart>[0-9]+)
                ([\-+] [0-9]+)*
            [\-+]
                (?P<episodenumberend>[0-9]+)
            \]
            [^\\/]*$''',

        # foo - [012]
        '''^((?P<seriesname>.+?)[ \._\-])?
            \[
            (?P<episodenumber>[0-9]+)
            \]
            [^\\/]*$''',
        # foo.s0101, foo.0201
        '''^(?P<seriesname>.+?)[ \._\-]
            [Ss](?P<seasonnumber>[0-9]{2})
            [\.\- ]?
            (?P<episodenumber>[0-9]{2})
            [^0-9]*$''',

        # foo.1x09*
        '''^((?P<seriesname>.+?)[ \._\-])?
            \[?
            (?P<seasonnumber>[0-9]+)
            [xX]
            (?P<episodenumber>[0-9]+)
            \]?
            [^\\/]*$''',

        # foo.s01.e01, foo.s01_e01, "foo.s01 - e01"
        '''^((?P<seriesname>.+?)[ \._\-])?
            \[?
            [Ss](?P<seasonnumber>[0-9]+)[ ]?[\._\- ]?[ ]?
            [Ee]?(?P<episodenumber>[0-9]+)
            \]?
            [^\\/]*$''',

        # foo.2010.01.02.etc
        '''
            ^((?P<seriesname>.+?)[ \._\-])?
            (?P<year>\d{4})
            [ \._\-]
            (?P<month>\d{2})
            [ \._\-]
            (?P<day>\d{2})
            [^\/]*$''',

        # foo - [01.09]
        '''^((?P<seriesname>.+?))
            [ \._\-]?
            \[
            (?P<seasonnumber>[0-9]+?)
            [.]
            (?P<episodenumber>[0-9]+?)
            \]
            [ \._\-]?
            [^\\/]*$''',

        # Foo - S2 E 02 - etc
        '''^(?P<seriesname>.+?)[ ]?[ \._\-][ ]?
            [Ss](?P<seasonnumber>[0-9]+)[\.\- ]?
            [Ee]?[ ]?(?P<episodenumber>[0-9]+)
            [^\\/]*$''',

        # Show - Episode 9999 [S 12 - Ep 131] - etc
        '''(?P<seriesname>.+)
            [ ]-[ ]
            [Ee]pisode[ ]\d+
            [ ]
            \[
            [sS][ ]?(?P<seasonnumber>\d+)
            ([ ]|[ ]-[ ]|-)
            ([eE]|[eE]p)[ ]?(?P<episodenumber>\d+)
            \]
            .*$
            ''',

        # show name 2 of 6 - blah
        '''^(?P<seriesname>.+?)
            [ \._\-]
            (?P<episodenumber>[0-9]+)
            of
            [ \._\-]?
            \d+
            ([\._ -]|$|[^\\/]*$)
            ''',

        # Show.Name.Part.1.and.Part.2
        '''^(?i)
            (?P<seriesname>.+?)
            [ \._\-]
            (?:part|pt)?[\._ -]
            (?P<episodenumberstart>[0-9]+)
            (?:
              [ \._-](?:and|&|to)
              [ \._-](?:part|pt)?
              [ \._-](?:[0-9]+))*
            [ \._-](?:and|&|to)
            [ \._-]?(?:part|pt)?
            [ \._-](?P<episodenumberend>[0-9]+)
            [\._ -][^\\/]*$
            ''',

        # Show.Name.Part1
        '''^(?P<seriesname>.+?)
            [ \\._\\-]
            [Pp]art[ ](?P<episodenumber>[0-9]+)
            [\\._ -][^\\/]*$
            ''',

        # show name Season 01 Episode 20
        '''^(?P<seriesname>.+?)[ ]?
            [Ss]eason[ ]?(?P<seasonnumber>[0-9]+)[ ]?
            [Ee]pisode[ ]?(?P<episodenumber>[0-9]+)
            [^\\/]*$''',

        # foo.103*
        '''^(?P<seriesname>.+)[ \._\-]
            (?P<seasonnumber>[0-9]{1})
            (?P<episodenumber>[0-9]{2})
            [\._ -][^\\/]*$''',

        # foo.0103*
        '''^(?P<seriesname>.+)[ \._\-]
            (?P<seasonnumber>[0-9]{2})
            (?P<episodenumber>[0-9]{2,3})
            [\._ -][^\\/]*$''',

        # show.name.e123.abc
        '''^(?P<seriesname>.+?)
            [ \._\-]
            [Ee](?P<episodenumber>[0-9]+)
            [\._ -][^\\/]*$
            ''',
    ]

    # Search if we find a series
    for pattern in series_patterns:
        m = re.match(pattern, filename, re.VERBOSE | re.IGNORECASE)
        if not m:
            continue

        groupnames = m.groupdict().keys()
        series = {
            'type': 'episode',
            'show': None,
            'season': None,
            'episodes': None,
        }

        # Show name
        series['show'] = cleanRegexedName(m.group('seriesname'))

        # Season
        series['season'] = int(m.group('seasonnumber'))

        # Episodes
        if 'episodenumberstart' in groupnames:
            if m.group('episodenumberend'):
                start = int(m.group('episodenumberstart'))
                end = int(m.group('episodenumberend'))
                if start > end:
                    start, end = end, start
                series['episodes'] = list(range(start, end + 1))
            else:
                series['episodes'] = [int(m.group('episodenumberstart')), ]
        elif 'episodenumber' in groupnames:
            series['episodes'] = [int(m.group('episodenumber')), ]

        found['episode'] = series
        break

    # The patterns that will be used to search for a movie
    movies_patterns = [
        '''^(\(.*?\)|\[.*?\])?( - )?[ ]*?
            (?P<moviename>.*?)
            (dvdrip|xvid| cd[0-9]|dvdscr|brrip|divx|
            [\{\(\[]?(?P<year>[0-9]{4}))
            .*$
            ''',

        '''^(\(.*?\)|\[.*?\])?( - )?[ ]*?
            (?P<moviename>.+?)[ ]*?
            (?:[[(]?(?P<year>[0-9]{4})[])]?.*)?
            (?:\.[a-zA-Z0-9]{2,4})?$
            ''',
    ]

    # Search if we find a series
    for pattern in movies_patterns:
        m = re.match(pattern, filename, re.VERBOSE | re.IGNORECASE)
        if not m:
            continue

        groupnames = m.groupdict().keys()
        movie = {
            'type': 'movie',
            'title': None,
            'year': None,
        }

        # Movie title
        movie['title'] = cleanRegexedName(m.group('moviename'))

        # Year
        if 'year' in groupnames and m.group('year'):
            movie['year'] = m.group('year')

        found['movie'] = movie
        break

    if found:
        return found
 
    # Not found
    return False


##############################################################################
# Action to perform when the RESOLVE command is used.
def action_resolve(meta, oshash, size, duration):
    # Prepare the parameters
    meta = json.loads(meta)

    # Parse the filename to get more information
    parsed = parse_filename(meta['filename'])

    ##########################################################################
    # Internal class to represent resolution exceptions
    class ResolveException(Exception):
        pass

    ##########################################################################
    # Internal class to get OpenSubtitles XML-RPC API proxy
    class OpenSubtitlesAPI(object):
        _connected = False

        @classmethod
        def _connect(cls):
            if cls._connected:
                return

            # Initialize the connection to opensubtitles
            cls._proxy = xmlrpc.ServerProxy(
                'https://api.opensubtitles.org/xml-rpc')
            cls._login = cls._proxy.LogIn(
                '', '', 'en', 'TraktForVLC v2.0.0-devme')
            cls._connected = True

        @classmethod
        def check_hash(cls, *args, **kwargs):
            cls._connect()
            return cls._proxy.CheckMovieHash2(
                cls._login['token'], *args, **kwargs)

        @classmethod
        def insert_hash(cls, *args, **kwargs):
            cls._connect()
            return cls._proxy.InsertMovieHash(
                cls._login['token'], *args, **kwargs)
 
    ##########################################################################
    # Internal function to search by hash
    def search_hash():
        if not oshash:
            return
        
        LOGGER.info('Searching media using hash research')

        # Search for the files corresponding to this hash
        try:
            medias = OpenSubtitlesAPI.check_hash([oshash, ])
        except Exception as e:
            raise ResolveException(e)

        # If the hash is not in the results
        medias = medias['data'] if 'data' in medias else []
        if not oshash in medias:
            return

        # We're only interested in that hash
        medias = medias[oshash]
        
        if len(medias) == 1:
            # There is only one, so might as well be that one!
            media = medias[0]

            # Unless it's not the same type...
            if media['MovieKind'] not in parsed:
                return
        else:
            # Initialize media to None in case we don't find anything
            media = None

            # Search differently if it's an episode or a movie
            if 'episode' in parsed:
                episode = parsed['episode']

                # Define the prefix that characterize the show
                show_prefix = '"{}"'.format(episode['show'].lower())

                # And search if we find the first episode
                for m in medias:
                    if m['MovieKind'] != 'episode' or \
                            int(m['SeriesSeason']) != episode['season'] or \
                            int(m['SeriesEpisode']) != episode[
                                'episodes'][0] or \
                            not m['MovieName'].lower().startswith(
                                show_prefix):
                        continue
                    
                    media = m
                    break

                # If we reach here and still haven't got the episode, try to
                # see if we had maybe a typo in the name
                if not media:
                    def weight_episode(x):
                        return fuzzywuzzy.fuzz.ratio(
                            parsed['episode']['show'], re.sub(
                                '^\"([^"]*)\" .*$', '\\1', x['MovieName']))

                    # Use fuzzywuzzy to get the closest show name
                    closest = max(
                        (
                            m
                            for m in medias
                            if m['MovieKind'] == 'episode' and
                            int(m['SeriesSeason']) == episode['season'] and
                            int(m['SeriesEpisode']) == episode['episodes'][0]
                        ),
                        key=weight_episode,
                    )
                    if weight_episode(closest) >= .8:
                        media = closest
            
            if not media and 'movie' in parsed:
                movie = parsed['movie']

                media_name = movie.get('title')

                # Use fuzzywuzzy to get the closest movie name
                media = max(
                    medias,
                    key=lambda x: fuzzywuzzy.fuzz.ratio(
                        media_name, x['MovieName'])
                )

                imdb = imdbpie.Imdb(
                    exclude_episodes=True,
                )

                try:
                    result = imdb.get_title(
                        'tt{}'.format(media['MovieImdbID']))
                except Exception as e:
                    raise ResolveException(e)

                return imdb, result

        # If when reaching here we don't have the media, return None
        if not media:
            return

        # Else, we will need imdb for getting more detailed information on
        # the media; we'll exclude episodes if we know the media is a movie
        imdb = imdbpie.Imdb(
            exclude_episodes=(media['MovieKind'] == 'movie'),
        )

        try:
            result = imdb.get_title('tt{}'.format(media['MovieImdbID']))
        except Exception as e:
            raise ResolveException(e)

        # Find the media
        return imdb, result

    ##########################################################################
    # Internal function to search by text for a movie
    def search_text_movie():
        LOGGER.info('Searching media using text research on movie')
        movie = parsed['movie']
        
        # Initialize the imdb object to perform the research
        imdb = imdbpie.Imdb(
            exclude_episodes=True,
        )
        
        # Use imdb to search for the movie
        try:
            search = imdb.search_for_title(movie['title'])
        except Exception as e:
            raise ResolveException(e)
        
        if not search:
            return

        year_found = False
        for r in search:
            r['fuzz_ratio'] = fuzzywuzzy.fuzz.ratio(
                movie['title'], r['title'])
            if movie['year'] and \
                    not year_found and \
                    r['year'] == movie['year']:
                year_found = True

        if year_found:
            search = [r for r in search if r['year'] == movie['year']]

        if not duration:
            # If we don't have the movie duration, we won't be able to use it
            # to discriminate the movies, so just use the highest ratio
            max_ratio = max(r['fuzz_ratio'] for r in search)
            search = [r for r in search if r['fuzz_ratio'] == max_ratio]

            # Even if there is multiple with the highest ratio, only return one
            return imdb, imdb.get_title(search[0]['imdb_id'])

        # If we have the movie duration, we can use it to make the
        # research more precise, so we can be more gentle on the ratio
        sum_ratio = sum(r['fuzz_ratio'] for r in search)
        mean_ratio = sum_ratio / float(len(search))
        std_dev_ratio = math.sqrt(
            sum([
                math.pow(r['fuzz_ratio'] - mean_ratio, 2)
                for r in search
            ]) / float(len(search))
        )

        # Select only the titles over a given threshold
        threshold = mean_ratio + std_dev_ratio
        search = [r for r in search if r['fuzz_ratio'] >= threshold]

        # Now we need to get more information to identify precisely the movie
        for r in search:
            r['details'] = imdb.get_title(r['imdb_id'])

        # Try to get the closest movie using the movie duration if available
        closest = min(
            search,
            key=lambda x:
                abs(x['details']['base']['runningTimeInMinutes'] * 60. -
                    duration)
                if duration and
                x['details']['base']['runningTimeInMinutes'] is not None
                else sys.maxint
        )

        # Return the imdb information of the closest movie found
        return imdb, closest['details'], closest['fuzz_ratio']

    ##########################################################################
    # Internal function to search by text for an episode
    def search_text_episode():
        LOGGER.info('Searching media using text research on episode')
        ep = parsed['episode']

        # To allow to search on the tvdb
        tvdb = tvdb_api.Tvdb(
            cache=False,
            language='en',
        )

        # Perform the search, if nothing is found, there's a problem...
        try:
            series = tvdb.search(ep['show'])
        except Exception as e:
            raise ResolveException(e)

        if not series:
            return

        series = tvdb[series[0]['seriesName']]
        episode = series[ep['season']][ep['episodes'][0]]

        # Initialize the imdb object to perform the research
        imdb = imdbpie.Imdb(
            exclude_episodes=False,
        )
        
        # Use imdb to search for the series using its name or aliases
        search = None
        for seriesName in [series['seriesName'], ] + series['aliases']:
            try:
                search = imdb.search_for_title(seriesName)
            except Exception as e:
                raise ResolveException(e)

            # Filter the results by name and type
            search = [
                s for s in search
                if s['type'] == 'TV series' and
                s['title'] == seriesName
            ]

            # If there is still more than one, filter by year
            if len(search) > 1:
                search = [
                    s for s in search
                    if s['year'] == series['firstAired'][:4]
                ]

            # If we have a series, we can stop there!
            if search:
                break

        # If we did not find anything that matches
        if not search:
            return

        # Get the series' seasons and episodes
        series = imdb.get_title_episodes(search[0]['imdb_id'])
        for season in series['seasons']:
            if season['season'] != ep['season']:
                continue

            for episode in season['episodes']:
                if episode['episode'] != ep['episodes'][0]:
                    continue

                # id is using format /title/ttXXXXXX/
                return imdb, imdb.get_title(episode['id'][7:-1])

        # Not found
        return

    ##########################################################################
    # Internal function to search by text
    def search_text():
        search = None
        if 'episode' in parsed:
            try:
                search = search_text_episode()
            except ResolveException as e:
                LOGGER.warning(
                    'Exception when trying to search manually '
                    'for an episode: {}'.format(e))

        if not search and 'movie' in parsed:
            try:
                search = search_text_movie()
            except ResolveException as e:
                LOGGER.warning(
                    'Exception when trying to search manually '
                    'for a movie: {}'.format(e))
        
        return search

    ##########################################################################
    # Internal function to insert a hash
    def insert_hash(media, ratio):
        if not oshash or (parsed['type'] == 'movie' and ratio < 70.):
            return
        
        LOGGER.info('Sending movie hash information to opensubtitles')
        
        # Insert the movie hash if possible!
        media_duration = (
            duration * 1000.0
            if duration
            else media['base']['runningTimeInMinutes'] * 60. * 1000.
        )
        try:
            res = OpenSubtitlesAPI.insert_hash(
                [
                    {
                        'moviehash': oshash,
                        'moviebytesize': size,
                        'imdbid': media['base']['id'][9:-1],
                        'movietimems': media_duration,
                        'moviefilename': meta['filename'],
                    },
                ]
            )
        except Exception as e:
            raise ResolveException(e)
        
        LOGGER.info(res)
        if res['status'] != '200 OK':
            title = media['base']['title']
            if media['base']['titleType'] == 'tvEpisode':
                title = '{} - S{:02d}E{:02d} - {}'.format(
                    media['base']['parentTitle']['title'],
                    media['base']['season'],
                    media['base']['episode'],
                    title,
                )
            LOGGER.info('Unable to submit hash for \'{0}\': {1}'.format(
                title, res['status']))
        elif oshash in res['data']['accepted_moviehashes']:
            LOGGER.info('New hash submitted and accepted')
        else:
            LOGGER.info('New hash submitted but not accepted')

    ##########################################################################
    # Internal function to convert unicode strings to byte strings
    def byteify(input):
        if isinstance(input, dict):
            return {byteify(key): byteify(value)
                    for key, value in input.iteritems()}
        elif isinstance(input, list):
            return [byteify(element) for element in input]
        elif isinstance(input, unicode):
            return input.encode('utf-8')
        else:
            return input

    ##########################################################################
    # Logic of that function

    # To determine if we'll have to insert the hash
    should_insert_hash = False
    media = None

    # First search using the hash
    try:
        media = search_hash()
    except ResolveException as e:
        LOGGER.warning('Exception when trying to resolve the hash: {}'.format(
            e))

    # If not found, try using the information we can get from the metadata
    # and the file name
    if not media:
        should_insert_hash = True
        media = search_text()

    # If still not found, print an empty list, and return
    if not media:
        print('[]')
        return

    # Split the imdb object so we can reuse the one that has been instanciated
    # during the research
    ratio = media[2] if len(media) > 2 else 0
    imdb, media = media[:2]

    if media['base']['titleType'] == 'tvEpisode':
        parsed['type'] = 'episode'
    else:
        parsed['type'] = 'movie'

    # If we need to insert the hash, insert it for the first media found
    # only - OpenSubtitles does not allow duplicates, and it will still allow
    # for matches
    if oshash and should_insert_hash:
        try:
            insert_hash(media, ratio)
        except ResolveException as e:
            LOGGER.warning(
                'Exception when trying to insert the hash: {}'.format(e))

    # Return in the form of a list
    media_list = [media, ]

    # If it was an episode, and we had more episodes in the list...
    if parsed['type'] == 'episode' and len(parsed['episode']['episodes']) > 1:
        while len(media_list) < len(parsed['episode']['episodes']):
            # id is using format /title/ttXXXXXX/ - We just want the ttXXXXXX
            media = imdb.get_title(media['base']['nextEpisode'][7:-1])
            media_list.append(media)

    ##########################################################################
    # Print the JSON dump of the media list
    print(json.dumps(byteify(media_list), sort_keys=True,
                     indent=4, separators=(',', ': '),
                     ensure_ascii=False))


##############################################################################
# Action to perform when the DATE command is used.
def action_date(format, timezone, from_date, from_timezone, from_format):
    if not format:
        format = ['%Y-%m-%dT%H:%M:%S.%fZ', ]
    if from_date:
        if from_format in ['%s', '%s.%f']:
            from_date = float(from_date)
            from_dt = datetime.datetime.fromtimestamp(from_date)
        else:
            from_dt = datetime.datetime.strptime(from_date, from_format)
        if from_timezone:
            from_tz = pytz.timezone(from_timezone)
            from_dt = from_tz.localize(from_dt)
        else:
            from_dt = pytz.utc.localize(from_dt)
    else:
        from_dt = pytz.utc.localize(datetime.datetime.utcnow())

    if timezone:
        to_tz = pytz.timezone(timezone)
    else:
        to_tz = pytz.utc

    to_dt = from_dt.astimezone(to_tz)

    date = [
        {
            'format': f,
            'date': to_dt.strftime(f),
            'timezone': to_tz.zone,
        }
        for f in format
    ]
    if len(date) == 1:
        date = date[0]
    print(json.dumps(date, sort_keys=True,
          indent=4, separators=(',', ': '),
          ensure_ascii=False))


##############################################################################
# Action to perform when the RUNVLC command is used.
def action_runvlc(vlc_bin, parameters):
    # Try to find the VLC executable if it has not been passed as parameter
    if not vlc_bin:
        LOGGER.info('Searching for VLC binary...')
        vlc_bin = get_vlc()
    # If we still did not find it, cancel the installation as we will not be
    # able to complete it
    if not vlc_bin:
        raise RuntimeError(
            'VLC executable not found: use the --vlc parameter '
            'to specify VLC location')

    # Preparing the command
    command = [
        vlc_bin,
    ] + parameters

    LOGGER.info('Running command: {}'.format(
        subprocess.list2cmdline(command)))

    run = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        **run_as_user()
    )
    try:
        for line in iter(run.stdout.readline, b''):
            line = line.strip()
            print(line)
    except KeyboardInterrupt:
        run.kill()
    finally:
        run.stdout.close()

    sys.exit(run.wait())


##############################################################################
# Main method that will parse the command line arguments and run the function
# to perform the appropriate actions
def main():
    parser = argparse.ArgumentParser(
        description='TraktForVLC helper tool',
    )

    ##########################################################################
    # Parameters available for the tool in general
    parser.add_argument(
        '-V', '--version',
        action='version',
        version='%(prog)s {}'.format(__version__))
    parser.add_argument(
        '-d', '--debug',
        dest='loglevel',
        default='DEFAULT',
        action='store_const', const='DEBUG',
        help='Activate debug output')
    parser.add_argument(
        '-q', '--quiet',
        dest='loglevel',
        default='DEFAULT',
        action='store_const', const='ERROR',
        help='Only show errors or critical log messages')
    parser.add_argument(
        '--loglevel',
        dest='loglevel',
        default='DEFAULT',
        choices=('NOTSET', 'DEBUG', 'INFO',
                 'WARNING', 'ERROR', 'CRITICAL'),
        help='Define the specific log level')

    ##########################################################################
    # To define the commands available with the helper and separate them
    commands = parser.add_subparsers(
        help='Helper command',
        dest='command',
    )

    ##########################################################################
    # The INSTALL command to install TraktForVLC
    install_parser = commands.add_parser(
        'install',
        help='To install TraktForVLC',
    )

    ##########################################################################
    # The UNINSTALL command to uninstall TraktForVLC
    uninstall_parser = commands.add_parser(
        'uninstall',
        help='To uninstall TraktForVLC',
    )

    ##########################################################################
    # Options to be added for both the INSTALL and UNINSTALL commands
    for p in [install_parser, uninstall_parser]:
        p.add_argument(
            '--system',
            help='Use system directories instead of per-user',
            action='store_true',
        )
        p.add_argument(
            '-y', '--yes',
            help='Do not prompt, approve all changes automatically.',
            action='store_true',
        )
        p.add_argument(
            '-n', '--dry-run',
            help='Only perform a dry-run for the command (nothing actually '
                 'executed)',
            action='store_true',
        )
        p.add_argument(
            '--vlc-config-directory',
            dest='vlc_config',
            help='To specify manually where the VLC configuration directory is',
        )
        p.add_argument(
            '--vlc-lua-directory',
            dest='vlc_lua',
            help='To specify manually where the VLC LUA directory is',
        )
        p.add_argument(
            '--vlc',
            dest='vlc_bin',
            help='To specify manually where the VLC executable is',
        )

    ##########################################################################
    # The REQUESTS command to perform HTTP/HTTPS requests
    requests_parser = commands.add_parser(
        'requests',
        help='To perform HTTP/HTTPS requests',
    )
    requests_parser.add_argument(
        'method',
        help='The method to use for the request',
        type=str.upper,
        choices=[
            'GET',
            'POST',
        ],
    )
    requests_parser.add_argument(
        'url',
        help='The URL to perform the request to',
    )
    requests_parser.add_argument(
        '--headers',
        help='The headers to set for the request',
    )
    requests_parser.add_argument(
        '--data',
        help='The data to be sent with the request',
    )

    ##########################################################################
    # The RESOLVE command to get movie/series information from media details
    resolve_parser = commands.add_parser(
        'resolve',
        help='To get the movie/episode information from media details',
    )
    resolve_parser.add_argument(
        '--meta',
        help='The metadata provided by VLC',
    )
    resolve_parser.add_argument(
        '--hash',
        dest='oshash',
        help='The hash of the media for OpenSubtitles resolution',
    )
    resolve_parser.add_argument(
        '--size',
        type=float,
        help='The size of the media, in bytes',
    )
    resolve_parser.add_argument(
        '--duration',
        type=float,
        help='The duration of the media, in seconds',
    )

    ##########################################################################
    # The DATE command to perform operations on dates
    date_parser = commands.add_parser(
        'date',
        help='To perform operations on dates',
    )
    date_parser.add_argument(
        '--format',
        action='append',
        default=[],
        help='The format of the date to output',
    )
    date_parser.add_argument(
        '--timezone',
        help='Which timezone to use for the destination date',
    )
    date_parser.add_argument(
        '--from',
        dest='from_date',
        help='From which time to print the time',
    )
    date_parser.add_argument(
        '--from-timezone',
        help='Which timezone to use for the from date, if different than '
             'the destination date',
    )
    date_parser.add_argument(
        '--from-format',
        default='%s.%f',
        help='Format of the date passed in the from argument',
    )

    ##########################################################################
    # The RUNVLC command to run VLC in a subprocess and get its output
    runvlc_parser = commands.add_parser(
        'runvlc',
        help='To run VLC in a subprocess and get its output even from Windows',
    )
    runvlc_parser.add_argument(
        'parameters',
        nargs='*',
        default=[],
        help='The command line parameters to pass to VLC',
    )
    runvlc_parser.add_argument(
        '--vlc',
        dest='vlc_bin',
        help='To specify manually where the VLC executable is',
    )


    ##########################################################################
    # Parse the arguments
    args = parser.parse_args()

    ##########################################################################
    # Prepare the logger
    if args.loglevel == 'DEFAULT':
        if args.command in ['install', 'uninstall']:
            args.loglevel = 'INFO'
        else:
            args.loglevel = 'WARNING'

    log_level_value = getattr(logging, args.loglevel)
    logging.basicConfig(
        level=log_level_value,
        format='%(asctime)s::%(levelname)s::%(message)s')

    ##########################################################################
    # Search the function to be called to perform the action
    action = globals().get('action_{}'.format(args.command))
    if action is None:
        raise RuntimeError('No action found for command {}'.format(
            args.command))

    ##########################################################################
    # Prepare the parameters to be passed to that function
    params = vars(args)
    del params['loglevel']
    del params['command']
    
    ##########################################################################
    # Call the function
    action(**params)


if __name__ == '__main__':
    main()
