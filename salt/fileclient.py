'''
Classes that manage file clients
'''

# Import python libs
import contextlib
import logging
import hashlib
import os
import shutil
import string
import subprocess

# Import third party libs
import yaml

# Import salt libs
from salt.exceptions import MinionError, SaltReqTimeoutError
import salt.client
import salt.crypt
import salt.loader
import salt.payload
import salt.utils
import salt.utils.templates
import salt.utils.gzip_util
from salt._compat import (
    URLError, HTTPError, BaseHTTPServer, urlparse, urlunparse, url_open,
    url_passwd_mgr, url_auth_handler, url_build_opener, url_install_opener )

log = logging.getLogger(__name__)


def get_file_client(opts):
    '''
    Read in the ``file_client`` option and return the correct type of file
    server
    '''
    return {
        'remote': RemoteClient,
        'local': LocalClient
    }.get(opts['file_client'], RemoteClient)(opts)


class Client(object):
    '''
    Base class for Salt file interactions
    '''
    def __init__(self, opts):
        self.opts = opts
        self.serial = salt.payload.Serial(self.opts)

    def _check_proto(self, path):
        '''
        Make sure that this path is intended for the salt master and trim it
        '''
        if not path.startswith('salt://'):
            raise MinionError('Unsupported path: {0}'.format(path))
        return path[7:]

    def _file_local_list(self, dest):
        '''
        Helper util to return a list of files in a directory
        '''
        if os.path.isdir(dest):
            destdir = dest
        else:
            destdir = os.path.dirname(dest)

        filelist = set()

        for root, dirs, files in os.walk(destdir, followlinks=True):
            for name in files:
                path = os.path.join(root, name)
                filelist.add(path)

        return filelist

    @contextlib.contextmanager
    def _cache_loc(self, path, env='base'):
        '''
        Return the local location to cache the file, cache dirs will be made
        '''
        dest = os.path.join(self.opts['cachedir'],
                            'files',
                            env,
                            path)
        destdir = os.path.dirname(dest)
        cumask = os.umask(63)
        if not os.path.isdir(destdir):
            # remove destdir if it is a regular file to avoid an OSError when
            # running os.makedirs below
            if os.path.isfile(destdir):
                os.remove(destdir)
            os.makedirs(destdir)
        yield dest
        os.umask(cumask)

    def get_file(self, path, dest='', makedirs=False, env='base', gzip=None):
        '''
        Copies a file from the local files or master depending on
        implementation
        '''
        raise NotImplementedError

    def file_list_emptydirs(self, env='base'):
        '''
        List the empty dirs
        '''
        raise NotImplementedError

    def cache_file(self, path, env='base'):
        '''
        Pull a file down from the file server and store it in the minion
        file cache
        '''
        return self.get_url(path, '', True, env)

    def cache_files(self, paths, env='base'):
        '''
        Download a list of files stored on the master and put them in the
        minion file cache
        '''
        ret = []
        if isinstance(paths, str):
            paths = paths.split(',')
        for path in paths:
            ret.append(self.cache_file(path, env))
        return ret

    def cache_master(self, env='base'):
        '''
        Download and cache all files on a master in a specified environment
        '''
        ret = []
        for path in self.file_list(env):
            ret.append(self.cache_file('salt://{0}'.format(path), env))
        return ret

    def cache_dir(self, path, env='base', include_empty=False):
        '''
        Download all of the files in a subdir of the master
        '''
        ret = []
        path = self._check_proto(path)
        # We want to make sure files start with this *directory*, use
        # '/' explicitly because the master (that's generating the
        # list of files) only runs on POSIX
        if not path.endswith('/'):
            path = path + '/'

        log.info(
            'Caching directory \'{0}\' for environment \'{1}\''.format(
                path, env
            )
        )
        #go through the list of all files finding ones that are in
        #the target directory and caching them
        ret.extend([self.cache_file('salt://' + fn_, env)
                    for fn_ in self.file_list(env)
                    if fn_.strip() and fn_.startswith(path)])

        if include_empty:
            # Break up the path into a list containing the bottom-level
            # directory (the one being recursively copied) and the directories
            # preceding it
            #separated = string.rsplit(path, '/', 1)
            #if len(separated) != 2:
            #    # No slashes in path. (So all files in env will be copied)
            #    prefix = ''
            #else:
            #    prefix = separated[0]
            dest = salt.utils.path_join(
                self.opts['cachedir'],
                'files',
                env
            )
            for fn_ in self.file_list_emptydirs(env):
                if fn_.startswith(path):
                    minion_dir = '{0}/{1}'.format(dest, fn_)
                    if not os.path.isdir(minion_dir):
                        os.makedirs(minion_dir)
                    ret.append(minion_dir)
        return ret

    def cache_local_file(self, path, **kwargs):
        '''
        Cache a local file on the minion in the localfiles cache
        '''
        dest = os.path.join(self.opts['cachedir'], 'localfiles',
                            path.lstrip('/'))
        destdir = os.path.dirname(dest)

        if not os.path.isdir(destdir):
            os.makedirs(destdir)

        shutil.copyfile(path, dest)
        return dest

    def file_local_list(self, env='base'):
        '''
        List files in the local minion files and localfiles caches
        '''
        filesdest = os.path.join(self.opts['cachedir'], 'files', env)
        localfilesdest = os.path.join(self.opts['cachedir'], 'localfiles')

        fdest = self._file_local_list(filesdest)
        ldest = self._file_local_list(localfilesdest)
        return sorted(fdest.union(ldest))

    def file_list(self, env='base'):
        '''
        This function must be overwritten
        '''
        return []

    def dir_list(self, env='base'):
        '''
        This function must be overwritten
        '''
        return []

    def is_cached(self, path, env='base'):
        '''
        Returns the full path to a file if it is cached locally on the minion
        otherwise returns a blank string
        '''
        localsfilesdest = os.path.join(
            self.opts['cachedir'], 'localfiles', path.lstrip('/'))
        filesdest = os.path.join(
            self.opts['cachedir'], 'files', env, path.lstrip('salt://'))

        if os.path.exists(filesdest):
            return filesdest
        elif os.path.exists(localsfilesdest):
            return localsfilesdest

        return ''

    def list_states(self, env):
        '''
        Return a list of all available sls modules on the master for a given
        environment
        '''
        states = []
        for path in self.file_list(env):
            if path.endswith('.sls'):
                # is an sls module!
                if path.endswith('{0}init.sls'.format('/')):
                    states.append(path.replace('/', '.')[:-9])
                else:
                    states.append(path.replace('/', '.')[:-4])
        return states

    def get_state(self, sls, env):
        '''
        Get a state file from the master and store it in the local minion
        cache return the location of the file
        '''
        if '.' in sls:
            sls = sls.replace('.', '/')
        for path in ['salt://{0}.sls'.format(sls),
                     '/'.join(['salt:/', sls, 'init.sls'])]:
            dest = self.cache_file(path, env)
            if dest:
                return {'source': path, 'dest': dest}
        return {}

    def get_dir(self, path, dest='', env='base', gzip=None):
        '''
        Get a directory recursively from the salt-master
        '''
        # TODO: We need to get rid of using the string lib in here
        ret = []
        # Strip trailing slash
        path = string.rstrip(self._check_proto(path), '/')
        # Break up the path into a list containing the bottom-level directory
        # (the one being recursively copied) and the directories preceding it
        separated = string.rsplit(path, '/', 1)
        if len(separated) != 2:
            # No slashes in path. (This means all files in env will be copied)
            prefix = ''
        else:
            prefix = separated[0]

        # Copy files from master
        for fn_ in self.file_list(env):
            if fn_.startswith(path):
                # Prevent files in "salt://foobar/" (or salt://foo.sh) from
                # matching a path of "salt://foo"
                try:
                    if fn_[len(path)] != '/':
                        continue
                except IndexError:
                    continue
                # Remove the leading directories from path to derive
                # the relative path on the minion.
                minion_relpath = string.lstrip(fn_[len(prefix):], '/')
                ret.append(
                    self.get_file(
                        'salt://{0}'.format(fn_),
                        '{0}/{1}'.format(dest, minion_relpath),
                        True, env, gzip
                    )
                )
        # Replicate empty dirs from master
        for fn_ in self.file_list_emptydirs(env):
            if fn_.startswith(path):
                # Prevent an empty dir "salt://foobar/" from matching a path of
                # "salt://foo"
                try:
                    if fn_[len(path)] != '/':
                        continue
                except IndexError:
                    continue
                # Remove the leading directories from path to derive
                # the relative path on the minion.
                minion_relpath = string.lstrip(fn_[len(prefix):], '/')
                minion_mkdir = '{0}/{1}'.format(dest, minion_relpath)
                if not os.path.isdir(minion_mkdir):
                    os.makedirs(minion_mkdir)
                ret.append(minion_mkdir)
        ret.sort()
        return ret

    def get_url(self, url, dest, makedirs=False, env='base'):
        '''
        Get a single file from a URL.
        '''
        url_data = urlparse(url)
        if url_data.scheme == 'salt':
            return self.get_file(url, dest, makedirs, env)
        if dest:
            destdir = os.path.dirname(dest)
            if not os.path.isdir(destdir):
                if makedirs:
                    os.makedirs(destdir)
                else:
                    return ''
        else:
            dest = salt.utils.path_join(
                self.opts['cachedir'],
                'extrn_files',
                env,
                url_data.netloc,
                url_data.path
            )
            destdir = os.path.dirname(dest)
            if not os.path.isdir(destdir):
                os.makedirs(destdir)
        if url_data.username is not None \
                and url_data.scheme in ('http', 'https'):
            _, netloc = url_data.netloc.split('@', 1)
            fixed_url = urlunparse((url_data.scheme, netloc, url_data.path,
                url_data.params, url_data.query, url_data.fragment ))
            passwd_mgr = url_passwd_mgr()
            passwd_mgr.add_password(None, fixed_url, url_data.username, url_data.password)
            auth_handler = url_auth_handler(passwd_mgr)
            opener = url_build_opener(auth_handler)
            url_install_opener(opener)
        else:
            fixed_url = url
        try:
            with contextlib.closing(url_open(fixed_url)) as srcfp:
                with salt.utils.fopen(dest, 'wb') as destfp:
                    shutil.copyfileobj(srcfp, destfp)
            return dest
        except HTTPError as ex:
            raise MinionError('HTTP error {0} reading {1}: {3}'.format(
                ex.code,
                url,
                *BaseHTTPServer.BaseHTTPRequestHandler.responses[ex.code]))
        except URLError as ex:
            raise MinionError('Error reading {0}: {1}'.format(url, ex.reason))

    def get_template(
            self,
            url,
            dest,
            template='jinja',
            makedirs=False,
            env='base',
            **kwargs):
        '''
        Cache a file then process it as a template
        '''
        kwargs['env'] = env
        url_data = urlparse(url)
        sfn = self.cache_file(url, env)
        if not os.path.exists(sfn):
            return ''
        if template in salt.utils.templates.TEMPLATE_REGISTRY:
            data = salt.utils.templates.TEMPLATE_REGISTRY[template](
                sfn,
                **kwargs
            )
        else:
            log.error('Attempted to render template with unavailable engine '
                      '{0}'.format(template))
            return ''
        if not data['result']:
            # Failed to render the template
            log.error(
                'Failed to render template with error: {0}'.format(
                    data['data']
                )
            )
            return ''
        if not dest:
            # No destination passed, set the dest as an extrn_files cache
            dest = salt.utils.path_join(
                self.opts['cachedir'],
                'extrn_files',
                env,
                url_data.netloc,
                url_data.path
            )
        destdir = os.path.dirname(dest)
        if not os.path.isdir(destdir):
            if makedirs:
                os.makedirs(destdir)
            else:
                salt.utils.safe_rm(data['data'])
                return ''
        shutil.move(data['data'], dest)
        return dest


class LocalClient(Client):
    '''
    Use the local_roots option to parse a local file root
    '''
    def __init__(self, opts):
        Client.__init__(self, opts)

    def _find_file(self, path, env='base'):
        '''
        Locate the file path
        '''
        fnd = {'path': '',
               'rel': ''}
        if env not in self.opts['file_roots']:
            return fnd
        if path.startswith('|'):
            # The path arguments are escaped
            path = path[1:]
        for root in self.opts['file_roots'][env]:
            full = os.path.join(root, path)
            if os.path.isfile(full):
                fnd['path'] = full
                fnd['rel'] = path
                return fnd
        return fnd

    def get_file(self, path, dest='', makedirs=False, env='base', gzip=None):
        '''
        Copies a file from the local files directory into :param:`dest`
        gzip compression settings are ignored for local files
        '''
        path = self._check_proto(path)
        fnd = self._find_file(path, env)
        if not fnd['path']:
            return ''
        return fnd['path']

    def file_list(self, env='base'):
        '''
        Return a list of files in the given environment
        '''
        ret = []
        if env not in self.opts['file_roots']:
            return ret
        for path in self.opts['file_roots'][env]:
            for root, dirs, files in os.walk(path, followlinks=True):
                for fname in files:
                    ret.append(
                        os.path.relpath(
                            os.path.join(root, fname),
                            path
                        )
                    )
        return ret

    def file_list_emptydirs(self, env='base'):
        '''
        List the empty dirs in the file_roots
        '''
        ret = []
        if env not in self.opts['file_roots']:
            return ret
        for path in self.opts['file_roots'][env]:
            for root, dirs, files in os.walk(path, followlinks=True):
                if len(dirs) == 0 and len(files) == 0:
                    ret.append(os.path.relpath(root, path))
        return ret

    def dir_list(self, env='base'):
        '''
        List the dirs in the file_roots
        '''
        ret = []
        if env not in self.opts['file_roots']:
            return ret
        for path in self.opts['file_roots'][env]:
            for root, dirs, files in os.walk(path, followlinks=True):
                ret.append(os.path.relpath(root, path))
        return ret

    def hash_file(self, path, env='base'):
        '''
        Return the hash of a file, to get the hash of a file in the file_roots
        prepend the path with salt://<file on server> otherwise, prepend the
        file with / for a local file.
        '''
        ret = {}
        try:
            path = self._check_proto(path)
        except MinionError:
            if not os.path.isfile(path):
                err = 'Specified file {0} is not present to generate hash'
                log.warning(err.format(path))
                return ret
            else:
                with salt.utils.fopen(path, 'rb') as ifile:
                    ret['hsum'] = hashlib.md5(ifile.read()).hexdigest()
                ret['hash_type'] = 'md5'
                return ret
        path = self._find_file(path, env)['path']
        if not path:
            return {}
        ret = {}
        with salt.utils.fopen(path, 'rb') as ifile:
            ret['hsum'] = getattr(hashlib, self.opts['hash_type'])(
                ifile.read()).hexdigest()
        ret['hash_type'] = self.opts['hash_type']
        return ret

    def list_env(self, env='base'):
        '''
        Return a list of the files in the file server's specified environment
        '''
        return self.file_list(env)

    def master_opts(self):
        '''
        Return the master opts data
        '''
        return self.opts

    def ext_nodes(self):
        '''
        Return the metadata derived from the external nodes system on the local
        system
        '''
        if not self.opts['external_nodes']:
            return {}
        if not salt.utils.which(self.opts['external_nodes']):
            log.error(('Specified external nodes controller {0} is not'
                       ' available, please verify that it is installed'
                       '').format(self.opts['external_nodes']))
            return {}
        cmd = '{0} {1}'.format(self.opts['external_nodes'], self.opts['id'])
        ndata = yaml.safe_load(subprocess.Popen(
                               cmd,
                               shell=True,
                               stdout=subprocess.PIPE
                               ).communicate()[0])
        ret = {}
        if 'environment' in ndata:
            env = ndata['environment']
        else:
            env = 'base'

        if 'classes' in ndata:
            if isinstance(ndata['classes'], dict):
                ret[env] = list(ndata['classes'])
            elif isinstance(ndata['classes'], list):
                ret[env] = ndata['classes']
            else:
                return ret
        return ret


class RemoteClient(Client):
    '''
    Interact with the salt master file server.
    '''
    def __init__(self, opts):
        Client.__init__(self, opts)
        self.auth = salt.crypt.SAuth(opts)
        self.sreq = salt.payload.SREQ(self.opts['master_uri'])

    def get_file(self, path, dest='', makedirs=False, env='base', gzip=None):
        '''
        Get a single file from the salt-master
        path must be a salt server location, aka, salt://path/to/file, if
        dest is omitted, then the downloaded file will be placed in the minion
        cache
        '''
        log.info('Fetching file \'{0}\''.format(path))
        d_tries = 0
        path = self._check_proto(path)
        load = {'path': path,
                'env': env,
                'cmd': '_serve_file'}
        if gzip:
            gzip = int(gzip)
            load['gzip'] = gzip

        fn_ = None
        if dest:
            destdir = os.path.dirname(dest)
            if not os.path.isdir(destdir):
                if makedirs:
                    os.makedirs(destdir)
                else:
                    return False
            fn_ = salt.utils.fopen(dest, 'wb+')
        while True:
            if not fn_:
                load['loc'] = 0
            else:
                load['loc'] = fn_.tell()
            try:
                data = self.auth.crypticle.loads(
                    self.sreq.send('aes',
                                   self.auth.crypticle.dumps(load),
                                   3,
                                   60)
                )
            except SaltReqTimeoutError:
                return ''

            if not data['data']:
                if not fn_ and data['dest']:
                    # This is a 0 byte file on the master
                    with self._cache_loc(data['dest'], env) as cache_dest:
                        dest = cache_dest
                        with salt.utils.fopen(cache_dest, 'wb+') as ofile:
                            ofile.write(data['data'])
                if 'hsum' in data and d_tries < 3:
                    # Master has prompted a file verification, if the
                    # verification fails, redownload the file. Try 3 times
                    d_tries += 1
                    with salt.utils.fopen(dest, 'rb') as fp_:
                        hsum = getattr(
                            hashlib,
                            data.get('hash_type', 'md5')
                        )(fp_.read()).hexdigest()
                        if hsum != data['hsum']:
                            log.warn('Bad download of file {0}, attempt {1} '
                                     'of 3'.format(path, d_tries))
                            continue
                break
            if not fn_:
                with self._cache_loc(data['dest'], env) as cache_dest:
                    dest = cache_dest
                    # If a directory was formerly cached at this path, then
                    # remove it to avoid a traceback trying to write the file
                    if os.path.isdir(dest):
                        salt.utils.rm_rf(dest)
                    fn_ = salt.utils.fopen(dest, 'wb+')
            if data.get('gzip', None):
                data = salt.utils.gzip_util.uncompress(data['data'])
            else:
                data = data['data']
            fn_.write(data)
        if fn_:
            fn_.close()
        return dest

    def file_list(self, env='base'):
        '''
        List the files on the master
        '''
        load = {'env': env,
                'cmd': '_file_list'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def file_list_emptydirs(self, env='base'):
        '''
        List the empty dirs on the master
        '''
        load = {'env': env,
                'cmd': '_file_list_emptydirs'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def dir_list(self, env='base'):
        '''
        List the dirs on the master
        '''
        load = {'env': env,
                'cmd': '_dir_list'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def hash_file(self, path, env='base'):
        '''
        Return the hash of a file, to get the hash of a file on the salt
        master file server prepend the path with salt://<file on server>
        otherwise, prepend the file with / for a local file.
        '''
        try:
            path = self._check_proto(path)
        except MinionError:
            if not os.path.isfile(path):
                err = 'Specified file {0} is not present to generate hash'
                log.warning(err.format(path))
                return {}
            else:
                ret = {}
                with salt.utils.fopen(path, 'rb') as ifile:
                    ret['hsum'] = hashlib.md5(ifile.read()).hexdigest()
                ret['hash_type'] = 'md5'
                return ret
        load = {'path': path,
                'env': env,
                'cmd': '_file_hash'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def list_env(self, env='base'):
        '''
        Return a list of the files in the file server's specified environment
        '''
        load = {'env': env,
                'cmd': '_file_list'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def master_opts(self):
        '''
        Return the master opts data
        '''
        load = {'cmd': '_master_opts'}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''

    def ext_nodes(self):
        '''
        Return the metadata derived from the external nodes system on the
        master.
        '''
        load = {'cmd': '_ext_nodes',
                'id': self.opts['id'],
                'opts': self.opts}
        try:
            return self.auth.crypticle.loads(
                self.sreq.send('aes',
                               self.auth.crypticle.dumps(load),
                               3,
                               60)
            )
        except SaltReqTimeoutError:
            return ''
