# Copyright (C) 2006 Novell Inc.  All rights reserved.
# This program is free software; it may be used, copied, modified
# and distributed under the terms of the GNU General Public Licence,
# either version 2, or (at your option) any later version.

import sys, os
import urllib2
from urlgrabber.grabber import URLGrabber, URLGrabError
from urlgrabber.mirror import MirrorGroup
from core import makeurl
from util import packagequery, cpio
import tempfile
try:
    from meter import TextMeter
except:
    TextMeter = None


def join_url(self, base_url, rel_url):
    """to override _join_url of MirrorGroup, because we want to
    pass full URLs instead of base URL where relative_url is added later...
    IOW, we make MirrorGroup ignore relative_url"""
    return base_url


class Fetcher:
    def __init__(self, cachedir = '/tmp', api_host_options = {}, urllist = [], http_debug = False, cookiejar = None, offline = False):

        __version__ = '0.1'
        __user_agent__ = 'osbuild/%s' % __version__

        # set up progress bar callback
        if sys.stdout.isatty() and TextMeter:
            self.progress_obj = TextMeter(fo=sys.stdout)
        else:
            self.progress_obj = None


        self.cachedir = cachedir
        self.urllist = urllist
        self.http_debug = http_debug
        self.offline = offline
        self.cpio = {}

        passmgr = urllib2.HTTPPasswordMgrWithDefaultRealm()
        for host in api_host_options.keys():
            passmgr.add_password(None, host, api_host_options[host]['user'], api_host_options[host]['pass'])
        openers = (urllib2.HTTPBasicAuthHandler(passmgr), )
        if cookiejar:
            openers += (urllib2.HTTPCookieProcessor(cookiejar), )
        self.gr = URLGrabber(user_agent=__user_agent__,
                            keepalive=1,
                            opener = urllib2.build_opener(*openers),
                            progress_obj=self.progress_obj,
                            failure_callback=(self.failureReport,(),{}),
                            )


    def failureReport(self, errobj):
        """failure output for failovers from urlgrabber"""

        #log(0, '%s: %s' % (errobj.url, str(errobj.exception)))
        #log(0, 'Trying other mirror.')
        print 'Trying openSUSE Build Service server for %s (%s), since it is not on %s.' \
                % (self.curpac, self.curpac.project, errobj.url.split('/')[2])
        raise errobj.exception


    def fetch(self, pac, prefix=''):
        # for use by the failure callback
        self.curpac = pac

        if self.offline:
            return True

        MirrorGroup._join_url = join_url
        mg = MirrorGroup(self.gr, pac.urllist)

        if self.http_debug:
            print
            print 'URLs to try for package \'%s\':' % pac
            print '\n'.join(pac.urllist)
            print

        (fd, tmpfile) = tempfile.mkstemp(prefix='osc_build')
        try:
            try:
                # it returns the filename
                ret = mg.urlgrab(pac.filename,
                                 filename = tmpfile,
                                 text = '%s(%s) %s' %(prefix, pac.project, pac.filename))
                self.move_package(tmpfile, pac.localdir, pac)
            except URLGrabError, e:
                if e.errno == 256:
                    self.cpio.setdefault(pac.project, {})[pac.name] = pac
                    return
                print
                print >>sys.stderr, 'Error:', e.strerror
                print >>sys.stderr, 'Failed to retrieve %s from the following locations (in order):' % pac.filename
                print >>sys.stderr, '\n'.join(pac.urllist)
                sys.exit(1)
        finally:
            os.close(fd)
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)

    def move_package(self, tmpfile, destdir, pac_obj = None):
        import shutil
        pkgq = packagequery.PackageQuery.query(tmpfile, extra_rpmtags=(1044, 1051, 1052))
        arch = pkgq.arch()
        # SOURCERPM = 1044
        if pkgq.filename_suffix == 'rpm' and not pkgq.getTag(1044):
            # NOSOURCE = 1051, NOPATCH = 1052
            if pkgq.getTag(1051) or pkgq.getTag(1052):
                arch = "nosrc"
            else:
                arch = "src"
        if pkgq.release():
            canonname = '%s-%s-%s.%s.%s' % (pkgq.name(), pkgq.version(), pkgq.release(), arch, pkgq.filename_suffix)
        else:
            canonname = '%s-%s.%s.%s' % (pkgq.name(), pkgq.version(), arch, pkgq.filename_suffix)
        fullfilename = os.path.join(destdir, canonname)
        if pac_obj is not None:
            pac_obj.filename = canonname
            pac_obj.fullfilename = fullfilename
        shutil.move(tmpfile, fullfilename)

    def dirSetup(self, pac):
        dir = os.path.join(self.cachedir, pac.localdir)
        if not os.path.exists(dir):
            try:
                os.makedirs(dir, mode=0755)
            except OSError, e:
                print >>sys.stderr, 'packagecachedir is not writable for you?'
                print >>sys.stderr, e
                sys.exit(1)


    def run(self, buildinfo):
        from urllib import quote_plus
        cached = 0
        all = len(buildinfo.deps)
        for i in buildinfo.deps:
            i.makeurls(self.cachedir, self.urllist)
            if os.path.exists(i.fullfilename):
                cached += 1
        miss = 0
        needed = all - cached
        if all:
            miss = 100.0 * needed / all
        print "%.1f%% cache miss. %d/%d dependencies cached.\n" % (miss, cached, all)
        done = 1
        for i in buildinfo.deps:
            i.makeurls(self.cachedir, self.urllist)
            if not os.path.exists(i.fullfilename):
                self.dirSetup(i)
                try:
                    # if there isn't a progress bar, there is no output at all
                    if not self.progress_obj:
                        print '%d/%d (%s) %s' % (done, needed, i.project, i.filename)
                    self.fetch(i)
                    if self.progress_obj:
                        print "  %d/%d\r" % (done,needed),
                        sys.stdout.flush()

                except KeyboardInterrupt:
                    print 'Cancelled by user (ctrl-c)'
                    print 'Exiting.'
                    sys.exit(0)
            done += 1
        for project, pkgs in self.cpio.iteritems():
            repo = pkgs.values()[0].repository
            query = [ 'binary=%s' % quote_plus(i) for i in pkgs.keys() ]
            query.append('view=cpio')
            try:
                (fd, tmparchive) = tempfile.mkstemp(prefix='osc_build_cpio')
                (fd, tmpfile) = tempfile.mkstemp(prefix='osc_build')
                url = makeurl(buildinfo.apiurl,
                              ['public/build', project, repo, buildinfo.buildarch, '_repository'],
                              query=query)
                self.gr.urlgrab(url, filename = tmparchive, text = 'fetching cpio for \'%s\'' % project)
                archive = cpio.CpioRead(tmparchive)
                archive.read()
                for hdr in archive:
                    if hdr.filename == '.errors':
                        import oscerr
                        archive.copyin_file(hdr.filename)
                        raise oscerr.APIError('CPIO archive is incomplete (see .errors file)')
                    pac = pkgs[hdr.filename.rsplit('.', 1)[0]]
                    archive.copyin_file(hdr.filename, os.path.dirname(tmpfile), os.path.basename(tmpfile))
                    self.move_package(tmpfile, pac.localdir, pac)
            finally:
                if os.path.exists(tmparchive):
                    os.unlink(tmparchive)
                if os.path.exists(tmpfile):
                    os.unlink(tmpfile)

def verify_pacs(pac_list):
    """Take a list of rpm filenames and run rpm -K on them.

       In case of failure, exit.

       Check all packages in one go, since this takes only 6 seconds on my Athlon 700
       instead of 20 when calling 'rpm -K' for each of them.
       """
    import subprocess

    if not pac_list:
        return

    # don't care about the return value because we check the
    # output anyway, and rpm always writes to stdout.

    # save locale first (we rely on English rpm output here)
    saved_LC_ALL = os.environ.get('LC_ALL')
    os.environ['LC_ALL'] = 'en_EN'

    o = subprocess.Popen(['rpm', '-K'] + pac_list, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, close_fds=True).stdout

    # restore locale
    if saved_LC_ALL: os.environ['LC_ALL'] = saved_LC_ALL
    else: os.environ.pop('LC_ALL')

    for line in o.readlines():

        if not 'OK' in line:
            print
            print >>sys.stderr, 'The following package could not be verified:'
            print >>sys.stderr, line
            sys.exit(1)

        if 'NOT OK' in line:
            print
            print >>sys.stderr, 'The following package could not be verified:'
            print >>sys.stderr, line

            if 'MISSING KEYS' in line:
                missing_key = line.split('#')[-1].split(')')[0]

                print >>sys.stderr, """
- If the key is missing, install it first.
  For example, do the following:
    gpg signkey PROJECT > file
  and, as root:
    rpm --import %(dir)s/keyfile-%(name)s

  Then, just start the build again.

- If you do not trust the packages, you should configure osc build for XEN or KVM

- You may use --no-verify to skip the verification (which is a risk for your system).
""" % {'name': missing_key,
       'dir': os.path.expanduser('~')}

            else:
                print >>sys.stderr, """
- If the signature is wrong, you may try deleting the package manually
  and re-run this program, so it is fetched again.
"""

            sys.exit(1)


