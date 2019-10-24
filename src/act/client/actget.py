#!/usr/bin/env python

"""
Get jobs from aCT.

Returns:
    1: No proxy found.
    2: One of the elements in job list is not a range.
    3: One of the elements in job list is not a valid ID.
    5: tmp directory not configured.
"""


import argparse
import sys
import shutil
import os
import logging

import act.client.jobmgr as jobmgr
import act.client.proxymgr as proxymgr
import act.client.clicommon as clicommon
from act.client.errors import TargetDirExistsError
from act.client.errors import InvalidJobRangeError
from act.client.errors import InvalidJobIDError
from act.client.errors import TmpConfigurationError
from act.client.errors import NoJobDirectoryError


def getLocalDir(jobdir, dirname=''):
    """
    Assemble destination directory for job results.

    Raises:
        TargetDirExistsError: Destination for job results already exists.
    """
    if dirname:
        dstDir = os.path.join(dirname, jobdir)
    else:
        dstDir = os.path.join(os.getcwd(), jobdir)
    if not os.path.exists(dstDir):
        return dstDir
    else:
        raise TargetDirExistsError(dstDir)


def main():
    # parse arguments
    parser = argparse.ArgumentParser(description='Get jobs from aCT')
    parser.add_argument('-j', '--jobs', default='',
            help='comma separated list of job IDs or ranges')
    parser.add_argument('-f', '--find', default='',
            help='get only jobs with matching (sub)string in their name')
    parser.add_argument('-s', '--state', default='',
            help='get only jobs with certain state')
    parser.add_argument('-v', '--verbose', action='store_true',
            help='show more information')
    parser.add_argument('-p', '--proxy', default=None,
            help='custom path to proxy certificate')
    parser.add_argument('-n', '--no-clean', action='store_true',
            help='do not clean jobs')
    args = parser.parse_args()

    # logging
    logFormat = "[%(asctime)s] [%(filename)s:%(lineno)d] [%(levelname)s] - %(message)s"
    if args.verbose:
        logging.basicConfig(format=logFormat, level=logging.DEBUG, stream=sys.stdout)
    else:
        logging.basicConfig(format=logFormat, level=logging.DEBUG, filename=os.devnull)

    # create a list of jobs to work on
    jobs = []
    if args.jobs:
        try:
            jobs = jobmgr.getIDsFromList(args.jobs)
        except InvalidJobRangeError as e:
            print "error: range '{}' is not a valid range".format(e.jobRange)
            sys.exit(2)
        except InvalidJobIDError as e:
            print "error: ID '{}' is not a valid ID".format(e.jobid)
            sys.exit(3)

    # get proxy ID given proxy
    proxyid = clicommon.getProxyIdFromProxy(args.proxy)

    # get job info
    manager = jobmgr.JobManager()
    try:
        results = manager.getJobs(proxyid, jobs, args.state, args.find)
    except TmpConfigurationError:
        print 'error: tmp directory not configured'
        sys.exit(5)

    if not results.jobdicts:
        print 'no jobs to get'
        sys.exit(0)

    # copy job results
    dontRemove = []
    for result in results.jobdicts:
        try:
            if result['dir']: # if there are job results in tmp
                dst_dirname = os.path.basename(os.path.normpath(result['name']))
                dstdir = getLocalDir(dst_dirname)
                shutil.copytree(result['dir'], dstdir)
                print 'Results stored at: {}'.format(dstdir)
            else:
                raise NoJobDirectoryError(result['dir'])

        except NoJobDirectoryError as e:
            print 'error: tmp results directory {} does not exist'.format(e.jobdir)
        except TargetDirExistsError as e:
            print 'error: job destination {} already exists'.format(e.dstdir)
            # don't clean job that could not be removed
            dontRemove.append(result['id'])

    # delete jobs that should not be removed from results
    for jobid in dontRemove:
        for result in results.jobdicts:
            if result['id'] == jobid:
                jobix = results.clientIDs.index(result['id'])
                del results.clientIDs[jobix]
                del results.arcIDs[jobix]
                del results.jobdicts[jobix]

    # clean jobs
    if not args.no_clean:
        manager.forceCleanJobs(results)


if __name__ == '__main__':
    main()


