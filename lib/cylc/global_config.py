#!/usr/bin/env python

import os, sys, re
from configobj import ConfigObj, ConfigObjError, get_extra_values, flatten_errors, Section
from validate import Validator
from print_cfg import print_cfg
from mkdir_p import mkdir_p
from copy import deepcopy
import atexit
import shutil
from tempfile import mkdtemp

try:
    any
except NameError:
    # any() appeared in Python 2.5
    def any(iterable):
        for entry in iterable:
            if entry:
                return True
        return False

class globalcfg( object ):
    """Handle global (all suites) site and user configuration for cylc.
    Legal items and default values are defined in a single configspec
    file.  Special comments in the configspec file denote items that can
    only be overridden by a site config file; otherwise a user config
    file can override site values (which override the defaults)."""

    def __init__( self ):
        """Load defaults, site, and user config files (in reverse order
        of precedence) to generate the global config structure; validate
        to catch errors; disallow user config of site-only items; expand
        environment variables, and create directories.""" 

        # location of the configspec file
        cfgspec = os.path.join( os.environ['CYLC_DIR'], 'conf', 'siterc', 'cfgspec' )

        # location of the site and user config files
        self.rcfiles = {
                'site' : os.path.join( os.environ['CYLC_DIR'], 'conf', 'siterc', 'site.rc' ),
                'user' : os.path.join( os.environ['HOME'], '.cylc', 'user.rc' )}

        # load the user file
        rc = self.rcfiles['user']
        try:
            self.usercfg = ConfigObj( infile=rc, configspec=cfgspec )
        except ConfigObjError, x:
            # (a non-existent user file does not trigger this exception)
            print >> sys.stderr, x
            raise SystemExit( "ERROR loading config file: " + rc )

        # generate a configobj with all defaults loaded from the configspec
        # (and call it self.cfg as we re-use it below for the final result)
        self.cfg = ConfigObj( configspec=cfgspec )
        self.validate( self.cfg ) # (validation loads the default settings)
        # check the user file for any attempt to override site-onlyitems
        self.block_user_cfg( self.usercfg, self.cfg, self.cfg.comments )

        # load the site file
        rc = self.rcfiles['site']
        try:
            self.sitecfg = ConfigObj( infile=rc, configspec=cfgspec, _inspec=False )
        except ConfigObjError, x:
            # (a non-existent site file does not trigger this exception)
            print >> sys.stderr, x
            raise SystemExit( "ERROR loading config file: " + rc )

        # merge site config into defaults (site takes precedence)
        self.cfg.merge( self.sitecfg )
        # now merge user config for final result (user takes precedence) 
        self.cfg.merge( self.usercfg )

        # now validate the final result to catch any errors
        self.validate( self.cfg )

        # expand out environment variables, create directories, etc.
        self.process()

    def write_rc( self, ftype=None ):
        """Generate initial site or user config files containing all
        available settings commented out.  In the user case the default
        values are obtained by any site settings into the configspec 
        defaults."""
        if ftype not in [ 'site', 'user' ]:
            raise SystemExit( "ERROR, illegal file type for write_rc(): " + ftype )

        target = self.rcfiles[ ftype ] 

        if os.path.exists( target ):
            raise SystemExit( "ERROR, file already exists: " + target )

        # cfgobj.write() will write a config file directly, but we want
        # add a file header, filter out some lines, and comment out all
        # the default settings ... so read into a string and process.

        if target == 'site':
            preamble = """
#_______________________________________________________________________
#       This is your cylc site configuration file, generated by:
#               'cylc get-global-config --write-site'
#-----------------------------------------------------------------------
#    Users can override these settings in $HOME/.cylc/user.rc, see:
#               'cylc get-global-config --write-user'
#-----------------------------------------------------------------------
# At the time of writing this file contained all available config items,
# commented out with '#==>', with initial values determined by the cylc
# system defaults in $CYLC_DIR/conf/site/cfgspec.
#-----------------------------------------------------------------------
# ** TO CUSTOMIZE, UNCOMMENT AND MODIFY SPECIFIC SETTINGS AS REQUIRED **
#          (just the items whose values you need to change)
#-----------------------------------------------------------------------
"""
        else:
            preamble = """
#_______________________________________________________________________
#       This is your cylc user configuration file, generated by:
#               'cylc get-global-config --write-user'
#-----------------------------------------------------------------------
# At the time of writing this file contained all available config items,
# commented out with '#==>', with initial values determined by the local
# site config file $CYLC_DIR/conf/site/siter.rc, or by the cylc system
# defaults in $CYLC_DIR/conf/site/cfgspec.
#-----------------------------------------------------------------------
# ** TO CUSTOMIZE, UNCOMMENT AND MODIFY SPECIFIC SETTINGS AS REQUIRED **
#          (just the items whose values you need to change)
#-----------------------------------------------------------------------
"""
        # start with a copy of the site config
        cfg = deepcopy( self.sitecfg )
        # validate to load defaults for items not set in site config
        self.validate( cfg )

        # write out all settings, commented out.
        outlines = preamble.split('\n')
        cfg.filename = None
        for line in cfg.write():
            if line.startswith( "#>" ):
                # omit comments specific to the spec file
                continue
            line = re.sub( '^(\s*)([^[#]+)$', '\g<1>#==> \g<2>', line )
            outlines.append(line)

        f = open( target, 'w' )
        for line in outlines:
            print >> f, line
        f.close()

        print "File written:", target
        print "See inside the file for usage instructions."

    def process( self ):
        # process temporary directory
        cylc_tmpdir = self.cfg['temporary directory']
        if not cylc_tmpdir:
            # use tempfile.mkdtemp() to create a new temp directory
            cylc_tmpdir = mkdtemp(prefix="cylc-")
            # self-cleanup
            atexit.register(lambda: shutil.rmtree(cylc_tmpdir))
            # now replace the original item
            self.cfg['temporary directory'] = cylc_tmpdir
        else:
            self.cfg['temporary directory'] = self.proc_dir( self.cfg['temporary directory'] )

        # expand environment variables and ~user in file paths
        for key,val in self.cfg['documentation']['files'].items():
            self.cfg['documentation']['files'][key] = os.path.expanduser( os.path.expandvars( val ))

        # expand variables in local directory paths, and create if necessary.
        self.cfg['task hosts']['local']['run directory'] = self.proc_dir( self.cfg['task hosts']['local']['run directory'] )
        self.cfg['task hosts']['local']['workspace directory'] = self.proc_dir( self.cfg['task hosts']['local']['workspace directory'] )
        self.cfg['pyro']['ports directory'] = self.proc_dir( self.cfg['pyro']['ports directory'] )

        # propagate host section defaults from the 'local' section
        for host in self.cfg['task hosts']:
            for key,val in self.cfg['task hosts'][host].items():
                if not val:
                    self.cfg['task hosts'][host][key] = self.cfg['task hosts']['local'][key]

    def proc_dir( self, path ):
        # expand environment variables and create dir if necessary.
        path = os.path.expandvars( os.path.expanduser( path ))
        try:
            mkdir_p( path )
        except Exception, x:
            print >> sys.stderr, x
            raise SystemExit( 'ERROR, illegal path? ' + dir )
        return path

    def validate( self, cfg ):
        # validate against the cfgspec and load defaults
        val = Validator()
        test = cfg.validate( val, preserve_errors=False, copy=True )
        if test != True:
            # Validation failed
            failed_items = flatten_errors( cfg, test )
            # Always print reason for validation failure
            for item in failed_items:
                sections, key, result = item
                print >> sys.stderr, ' ',
                for sec in sections:
                    print >> sys.stderr, sec, ' / ',
                print >> sys.stderr, key
                if result == False:
                    print >> sys.stderr, "ERROR, required item missing."
                else:
                    print >> sys.stderr, result
            raise SystemExit( "ERROR global config validation failed")
        extras = []
        for sections, name in get_extra_values( cfg ):
            extra = ' '
            for sec in sections:
                extra += sec + ' / '
            extras.append( extra + name )
        if len(extras) != 0:
            for extra in extras:
                print >> sys.stderr, '  ERROR, illegal entry:', extra 
            raise SystemExit( "ERROR illegal global config entry(s) found" )

    def block_user_cfg( self, usercfg, sitecfg, comments={}, sec_blocked=False ):
        """Check the comments for each item for the user exclusion indicator."""
        for item in usercfg:
            # iterate through sparse user config and check for attempts
            # to override any items marked '# SITE ONLY' in the spec.
            if isinstance( usercfg[item], dict ):
                if any( re.match( '^\s*# SITE ONLY\s*$', mem ) for mem in comments[item]):
                    # section blocked, but see if user actually attempts
                    # to set any items in it before aborting.
                    sb = True
                else:
                    sb = False
                self.block_user_cfg( usercfg[item], sitecfg[item], sitecfg[item].comments, sb )
            else:
                if any( re.match( '^\s*# SITE ONLY\s*$', mem ) for mem in comments[item]):
                    raise SystemExit( 'ERROR, item blocked from user override: ' + item )
                elif sec_blocked:
                    raise SystemExit( 'ERROR, section blocked from user override, item: ' + item )

    def dump( self, cfg_in=None ):
        if cfg_in:
            print_cfg( cfg_in, prefix='   ' )
        else:
            print_cfg( self.cfg, prefix='   ' )

    def get_task_work_dir( self, suite, task, host=None, owner=None ):
        # this goes under the top level workspace directory; it is
        # created on the fly, if necessary, by task job scripts.
        if host:
            work_root = self.cfg['task hosts'][host]['workspace directory']
        else:
            work_root = self.cfg['task hosts']['local']['workspace directory']
        if host or owner:
            # remote account: replace local home directory with '$HOME' 
            work_root  = re.sub( os.environ['HOME'], '$HOME', work_root )
        return os.path.join( work_root, suite, 'work', task )

    def get_suite_share_dir( self, suite, host=None, owner=None ):
        # this goes under the top level workspace directory; it is
        # created on the fly, if necessary, by task job scripts.
        if host:
            share_root = self.cfg['task hosts'][host]['workspace directory']
        else:
            share_root = self.cfg['task hosts']['local']['workspace directory']
        if host or owner:
            # remote account: replace local home directory, if present, with '$HOME' 
            share_root  = re.sub( os.environ['HOME'], '$HOME', share_root )
        return os.path.join( share_root, suite, 'share' )

    def get_suite_log_dir( self, suite, ext='suite', create=False ):
        path = os.path.join( self.cfg['task hosts']['local']['run directory'], suite, 'log', ext )
        if create:
            self.proc_dir( path )
        return path

    def get_task_log_dir( self, suite, host=None, owner=None, create=False ):
        log_root = None
        if host:
            log_root = self.cfg['task hosts'][host]['run directory']
        else:
            log_root = self.cfg['task hosts']['local']['run directory']
        if host or owner:
            # remote account: replace local home directory, if present, with '$HOME' 
            log_root  = re.sub( os.environ['HOME'], '$HOME', log_root )
        path = os.path.join( log_root, suite, 'log', 'job' )
        if create:
            self.proc_dir( path )
        return path

