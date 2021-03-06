import re
import time
import arc
from random import shuffle
from act.common.aCTProcess import aCTProcess
from act.common.aCTSignal import ExceptInterrupt
import multiprocessing, logging
import signal
import os

def KillPool(pool):
    # stop repopulating new child
    pool._state = multiprocessing.pool.TERMINATE
    pool._worker_handler._state = multiprocessing.pool.TERMINATE
    for p in pool._pool:
        try:
            os.kill(p.pid, signal.SIGKILL)
        except:
            pass
    # .is_alive() will reap dead process
    while any(p.is_alive() for p in pool._pool):
        pass
    pool.terminate()

class JobConv:

    def __init__(self):
        self.jobattrmap = {int: 'integer',
                      str: 'varchar(255)',
                      arc.JobState: 'varchar(255)',
                      arc.StringList: 'varchar(1024)',
                      arc.URL: 'varchar(255)',
                      arc.Period: 'int',
                      arc.Time: 'datetime',
                      arc.StringStringMap: 'varchar(1024)'}
        ignoremems=['STDIN',
                    'STDOUT',
                    'STDERR',
                    'STAGEINDIR',
                    'STAGEOUTDIR',
                    'SESSIONDIR',
                    'JOBLOG',
                    'JOBDESCRIPTION',
                    'JobDescriptionDocument']

        # Attributes of Job class mapped to DB column type
        self.jobattrs={}
        j=arc.Job()
        for i in dir(j):
            if re.match('^__',i):
                continue
            if i in ignoremems:
                continue
            if type(getattr(j, i)) in self.jobattrmap:
                self.jobattrs[i] = type(getattr(j, i))

    def db2job(self,dbinfo):
        '''
        Convert a dictionary of DB key value into arc Job object
        '''
        if dbinfo is None:
            return None
        j = arc.Job()
        for attr in self.jobattrs:
            if attr not in dbinfo or dbinfo[attr] is None:
                continue
            # Some object types need special treatment
            if self.jobattrs[attr] == arc.StringList:
                l = arc.StringList()
                for item in dbinfo[attr].split('|'):
                    l.append(item)
                setattr(j, attr, l)
                continue
            if self.jobattrs[attr] == arc.StringStringMap:
                m = arc.StringStringMap()
                d = eval(dbinfo[attr])
                if not isinstance(d, dict):
                    continue
                for (k,v) in d.items():
                    m[k] = v
                setattr(j, attr, m)
                continue

            setattr(j, attr, self.jobattrs[attr](str(dbinfo[attr])))
        return j

    def job2db(self,job):
        '''
        Convert an arc Job object to a dictionary of column name: value
        '''
        d = {}
        for attr in self.jobattrs:
            if self.jobattrs[attr] == int or self.jobattrs[attr] == str:
                d[attr] = str(getattr(job, attr))[:250]
            elif self.jobattrs[attr] == arc.JobState:
                d[attr] = getattr(job, attr).GetGeneralState()
            elif self.jobattrs[attr] == arc.StringList:
                d[attr] = '|'.join(getattr(job, attr))[:1000]
            elif self.jobattrs[attr] == arc.URL:
                d[attr] = getattr(job, attr).str().replace(r'\2f',r'/')
            elif self.jobattrs[attr] == arc.Period:
                d[attr] = str(getattr(job, attr).GetPeriod())
            elif self.jobattrs[attr] == arc.Time:
                if getattr(job, attr).GetTime() != -1:
                    # Use UTC time but strip trailing Z since mysql doesn't like it
                    t = str(getattr(job, attr).str(arc.UTCTime))
                    d[attr] = re.sub('Z$', '', t)
            elif self.jobattrs[attr] == arc.StringStringMap:
                ssm = getattr(job, attr)
                tmpdict = dict(zip(ssm.keys(), ssm.values()))
                d[attr] = str(tmpdict)[:1000]
            # Force everything to ASCII
                        # Force everything to ASCII
            if attr in d:
                d[attr] = ''.join([i for i in d[attr] if ord(i) < 128])
        return d

def Submit(id, appjobid, jobdescstr, ucproxy, timeout):

    global queuelist
    global usercred

    # get the submission logger
    #log = logger()
    log = logging.getLogger()

    if len(queuelist) == 0  :
        log.error("%s: no cluster free for submission" % appjobid)
        return None

    #cred_type=arc.initializeCredentialsType(arc.initializeCredentialsType.SkipCredentials)
    #uc=arc.UserConfig(cred_type)
    uc=usercred

    uc.CredentialString(ucproxy)

    jobdescs = arc.JobDescriptionList()
    if not jobdescstr or not arc.JobDescription_Parse(jobdescstr, jobdescs):
        log.error("%s: Failed to prepare job description" % appjobid)
        return None

    # Do brokering among the available queues
    jobdesc = jobdescs[0]
    broker = arc.Broker(uc, jobdesc, "Random")
    targetsorter = arc.ExecutionTargetSorter(broker)
    for target in queuelist:
        log.debug("%s: considering target %s:%s" % (appjobid, target.ComputingService.Name, target.ComputingShare.Name))

        # Adding an entity performs matchmaking and brokering
        targetsorter.addEntity(target)

    if len(targetsorter.getMatchingTargets()) == 0:
        log.error("%s: no clusters satisfied job description requirements" % appjobid)
        return None

    targetsorter.reset() # required to reset iterator, otherwise we get a seg fault
    selectedtarget = targetsorter.getCurrentTarget()
    # Job object will contain the submitted job
    job = arc.Job()
    submitter = arc.Submitter(uc)
    if submitter.Submit(selectedtarget, jobdesc, job) != arc.SubmissionStatus.NONE:
        log.error("%s: Submission failed" % appjobid)
        return None

    jconv = JobConv()
    return jconv.job2db(job)

class aCTSubmitter(aCTProcess):

    def submit(self):
        """
        Main function to submit jobs.
        """

        global queuelist

        # check for stopsubmission flag
        if self.conf.get(['downtime','stopsubmission']) == "true":
            self.log.info('Submission suspended due to downtime')
            return 0

        # Get cluster host and queue: cluster/queue
        clusterhost = clusterqueue = None
        if self.cluster:
            cluster = self.cluster
            if cluster.find('://') == -1:
                cluster = 'gsiftp://' + cluster
            clusterurl = arc.URL(cluster)
            clusterhost = clusterurl.Host()
            clusterqueue = clusterurl.Path()[1:] # strip off leading slash

        # Apply fair-share
        if self.cluster:
            fairshares = self.db.getArcJobsInfo("arcstate='tosubmit' and clusterlist like '%"+self.cluster+"%'", ['fairshare', 'proxyid'])
        else:
            fairshares = self.db.getArcJobsInfo("arcstate='tosubmit' and clusterlist=''", ['fairshare', 'proxyid'])

        if not fairshares:
            self.log.info('Nothing to submit')
            return 0

        # split by proxy for GU queues
        fairshares = list(set([(p['fairshare'], p['proxyid']) for p in fairshares]))
        # For proxy bug - see below
        shuffle(fairshares)
        count = 0

        for fairshare, proxyid in fairshares:

            try:
                # catch any exceptions here to avoid leaving lock
                if self.cluster:
                    # Lock row for update in case multiple clusters are specified
                    #jobs=self.db.getArcJobsInfo("arcstate='tosubmit' and ( clusterlist like '%{0}' or clusterlist like '%{0},%' ) and fairshare='{1}' order by priority desc limit 10".format(self.cluster, fairshare),
                    jobs=self.db.getArcJobsInfo("arcstate='tosubmit' and ( clusterlist like '%{0}' or clusterlist like '%{0},%' ) and fairshare='{1}' and proxyid='{2}' limit 10".format(self.cluster, fairshare, proxyid),
                                                columns=["id", "jobdesc", "appjobid", "priority", "proxyid", "clusterlist"], lock=True)
                    if jobs:
                        self.log.debug("started lock for writing %d jobs"%len(jobs))
                else:
                    jobs=self.db.getArcJobsInfo("arcstate='tosubmit' and clusterlist='' and fairshare='{0} and proxyid={1}' limit 10".format(fairshare, proxyid),
                                                columns=["id", "jobdesc", "appjobid", "priority", "proxyid", "clusterlist"])
                # mark submitting in db
                jobs_taken=[]
                for j in jobs:
                    jd={'cluster': self.cluster, 'arcstate': 'submitting', 'tarcstate': self.db.getTimeStamp()}
                    self.db.updateArcJobLazy(j['id'],jd)
                    jobs_taken.append(j)
                jobs=jobs_taken

            finally:
                if self.cluster:
                    try:
                        self.db.Commit(lock=True)
                        self.log.debug("ended lock")
                    except:
                        self.log.warning("Failed to release DB lock")
                else:
                    self.db.Commit()

            if len(jobs) == 0:
                #self.log.debug("No jobs to submit")
                continue
            self.log.info("Submitting %d jobs for fairshare %s and proxyid %d" % (len(jobs), fairshare, proxyid))

            # max waiting priority
            try:
                maxpriowaiting = max(jobs,key = lambda x : x['priority'])['priority']
            except:
                maxpriowaiting = 0
            self.log.info("Maximum priority of waiting jobs: %d" % maxpriowaiting)

            # Query infosys - either local or index
            if self.cluster:
                if self.cluster.find('://') != -1:
                    aris = arc.URL(self.cluster)
                else:
                    aris = arc.URL('gsiftp://%s' % self.cluster)
                if aris.Protocol() == 'https':
                    aris.ChangePath('/arex')
                    infoendpoints = [arc.Endpoint(aris.str(), arc.Endpoint.COMPUTINGINFO, 'org.ogf.glue.emies.resourceinfo')]
                elif aris.Protocol() == 'local':
                    infoendpoints = [arc.Endpoint(aris.str(), arc.Endpoint.COMPUTINGINFO, 'org.nordugrid.local')]
                else:
                    aris = 'ldap://'+aris.Host()+'/mds-vo-name=local,o=grid'
                    infoendpoints = [arc.Endpoint(aris, arc.Endpoint.COMPUTINGINFO, 'org.nordugrid.ldapng')]
            else:
                giises = self.conf.getList(['atlasgiis','item'])
                infoendpoints = []
                for g in giises:
                    # Specify explicitly EGIIS
                    infoendpoints.append(arc.Endpoint(str(g), arc.Endpoint.REGISTRY, "org.nordugrid.ldapegiis"))

            # Set UserConfig credential for querying infosys
            proxystring = str(self.db.getProxy(proxyid))
            self.uc.CredentialString(proxystring)
            global usercred
            usercred = self.uc
            # retriever contains a list of CE endpoints
            retriever = arc.ComputingServiceRetriever(self.uc, infoendpoints)
            retriever.wait()
            # targets is the list of queues
            # parse target.ComputingService.ID for the CE hostname
            # target.ComputingShare.Name is the queue name
            targets = retriever.GetExecutionTargets()

            # Filter only sites for this process
            queuelist=[]
            for target in targets:
                if not target.ComputingService.ID:
                    self.log.info("Target %s does not have ComputingService ID defined, skipping" % target.ComputingService.Name)
                    continue
                # If EMI-ES infoendpoint, force EMI-ES submission
                if infoendpoints[0].InterfaceName == 'org.ogf.glue.emies.resourceinfo' and target.ComputingEndpoint.InterfaceName != 'org.ogf.glue.emies.activitycreation':
                    self.log.debug("Rejecting target interface %s because not EMI-ES" % target.ComputingEndpoint.InterfaceName)
                    continue
                # Check for matching host and queue
                targethost = re.sub(':arex$', '', re.sub('urn:ogf:ComputingService:', '', target.ComputingService.ID))
                targetqueue = target.ComputingShare.Name
                if clusterhost and targethost != clusterhost:
                    self.log.debug('Rejecting target host %s as it does not match %s' % (targethost, clusterhost))
                    continue
                if clusterqueue and targetqueue != clusterqueue:
                    self.log.debug('Rejecting target queue %s as it does not match %s' % (targetqueue, clusterqueue))
                    continue
                if targetqueue in self.conf.getList(['queuesreject','item']):
                    self.log.debug('Rejecting target queue %s in queuesreject list' % targetqueue)
                    continue
                elif targethost in self.conf.getList(['clustersreject','item']):
                    self.log.debug('Rejecting target host %s in clustersreject list' % targethost)
                    continue
                else:
                    # tmp hack
                    target.ComputingShare.LocalWaitingJobs = 0
                    target.ComputingShare.PreLRMSWaitingJobs = 0
                    target.ExecutionEnvironment.CPUClockSpeed = 2000
                    qjobs=self.db.getArcJobsInfo("cluster='" +str(self.cluster)+ "' and  arcstate='submitted' and fairshare='%s'" % fairshare, ['id','priority'])
                    rjobs=self.db.getArcJobsInfo("cluster='" +str(self.cluster)+ "' and  arcstate='running' and fairshare='%s'" % fairshare, ['id'])

                    # max queued priority
                    try:
                        maxprioqueued = max(qjobs,key = lambda x : x['priority'])['priority']
                    except:
                        maxprioqueued = 0
                    self.log.info("Max priority queued: %d" % maxprioqueued)

                    # Limit number of submitted jobs using configuration or default (0.15 + 100/num of shares)/num CEs
                    # Note: assumes only a few shares are used
                    qfraction = float(self.conf.get(['jobs', 'queuefraction'])) if self.conf.get(['jobs', 'queuefraction']) else 0.15
                    qoffset = int(self.conf.get(['jobs', 'queueoffset'])) if self.conf.get(['jobs', 'queueoffset']) else 100
                    jlimit = (len(rjobs) * qfraction + qoffset/len(fairshares)) / len(jobs[0]['clusterlist'].split(','))
                    self.log.debug("running %d, queued %d, queue limit %d" % (len(rjobs), len(qjobs), jlimit))
                    if str(self.cluster).find('arc-boinc-0') != -1:
                        jlimit = len(rjobs)*0.15 + 400
                    if str(self.cluster).find('XXXpikolit') != -1:
                        jlimit = len(rjobs)*0.15 + 100
                    if str(self.cluster).find('arc05.lcg') != -1:
                        jlimit = len(rjobs)*0.15 + 400
                    target.ComputingShare.PreLRMSWaitingJobs=len(qjobs)
                    if len(qjobs) < jlimit or ( ( maxpriowaiting > maxprioqueued ) and ( maxpriowaiting > 10 ) ) :
                        if maxpriowaiting > maxprioqueued :
                            self.log.info("Overriding limit, maxpriowaiting: %d > maxprioqueued: %d" % (maxpriowaiting, maxprioqueued))
                        queuelist.append(target)
                        self.log.debug("Adding target %s:%s" % (targethost, targetqueue))
                    else:
                        self.log.info("%s/%s already at limit of submitted jobs for fairshare %s" % (targethost, targetqueue, fairshare))

            # check if any queues are available, if not leave and try again next time
            if not queuelist:
                self.log.info("No free queues available")
                self.db.Commit()
                continue

            self.log.info("start submitting")

            # Just run one thread for each job in sequence. Strange things happen
            # when trying to create a new UserConfig object for each thread.
            tasks = []
            for j in jobs:
                self.log.debug("%s: preparing submission" % j['appjobid'])
                jobdescstr = str(self.db.getArcJobDescription(str(j['jobdesc'])))
                jobdescs = arc.JobDescriptionList()
                if not jobdescstr or not arc.JobDescription_Parse(jobdescstr, jobdescs):
                    self.log.error("%s: Failed to prepare job description" % j['appjobid'])
                    continue
                tasks.append((j['id'], j['appjobid'], jobdescstr, proxystring, int(self.conf.get(['atlasgiis','timeout'])) ))
                count=count+1

            npools=1
            if any(s in self.cluster for s in self.conf.getList(['parallelsubmit','item'])):
                npools=int(self.conf.get(['parallelsubmit','npools']))
            self.log.debug("Starting submitters: %s" % npools)

            pool = multiprocessing.Pool(npools)
            #results = []
            #for task in tasks:
            #    result = pool.apply_async(Submit,(task))
            #    results.append(result)
            # Submit in workers
            results = [pool.apply_async(Submit, (t)) for t in tasks]

            # timeout per submission
            timeout = 60
            stopflag = False
            for result,task in zip(results,tasks):
                try:
                    jdb = result.get(timeout)
                    jconv = JobConv()
                    job = jconv.db2job(jdb)
                except multiprocessing.TimeoutError:
                    self.log.error("%s: submission timeout: exit and try again" % task[1])
                    # abort submission if Submit process is stuck
                    #pool.terminate()
                    KillPool(pool)
                    pool.join()
                    stopflag = True
                    # reduce timeout to finish quickly
                    timeout = 0.1
                    continue
                if job is None:
                    self.log.error("%s: no job defined for %d" % (task[1], task[0]))
                    continue
                jd={}
                jd['arcstate']='submitted'
                # initial offset to 1 minute to force first status check
                jd['tarcstate']=self.db.getTimeStamp(time.time()-int(self.conf.get(['jobs','checkinterval']))+120)
                # extract hostname of cluster (depends on JobID being a URL)
                self.log.info("%s: job id %s" % (task[1], job.JobID))
                jd['cluster']=self.cluster
                self.db.updateArcJobLazy(task[0],jd,job)
            if not stopflag:
                pool.terminate()
                pool.join()
            else:
                # stop submitting, gsiftp connection problem likely
                raise ExceptInterrupt(15)

            self.log.info("threads finished")
            # commit transaction to release row locks
            self.db.Commit()

            # still proxy bug
            raise ExceptInterrupt(15)

        self.log.info("end submitting")

        return count


    def checkFailedSubmissions(self):

        jobs=self.db.getArcJobsInfo("arcstate='submitting' and cluster='"+self.cluster+"'", ["id"])

        # TODO query GIIS for job name specified in description to see if job
        # was really submitted or not
        for j in jobs:
            # set to toresubmit and the application should figure out what to do
            self.db.updateArcJob(j['id'], {"arcstate": "toresubmit",
                                           "tarcstate": self.db.getTimeStamp()})

    def processToCancel(self):

        if self.cluster:
            jobstocancel = self.db.getArcJobs("arcstate='tocancel' and (cluster='{0}' or clusterlist like '%{0}' or clusterlist like '%{0},%')".format(self.cluster))
        else:
            jobstocancel = self.db.getArcJobs("arcstate='tocancel' and cluster=''")
        if not jobstocancel:
            return

        self.log.info("Cancelling %i jobs" % sum(len(v) for v in jobstocancel.values()))
        for proxyid, jobs in jobstocancel.items():
            self.uc.CredentialString(str(self.db.getProxy(proxyid)))

            job_supervisor = arc.JobSupervisor(self.uc, [j[2] for j in jobs])
            job_supervisor.Update()
            job_supervisor.Cancel()

            notcancelled = job_supervisor.GetIDsNotProcessed()

            for (id, appjobid, job, created) in jobs:

                if not job.JobID:
                    # Job not submitted
                    self.log.info("%s: Marking unsubmitted job cancelled" % appjobid)
                    self.db.updateArcJob(id, {"arcstate": "cancelled",
                                              "tarcstate": self.db.getTimeStamp()})

                elif job.JobID in notcancelled:
                    if job.State == arc.JobState.UNDEFINED:
                        # If longer than one hour since submission assume job never made it
                        if arc.Time(int(created.strftime("%s"))) + arc.Period(3600) < arc.Time():
                            self.log.warning("%s: Assuming job %s is lost and marking as cancelled" % (appjobid, job.JobID))
                            self.db.updateArcJob(id, {"arcstate": "cancelled",
                                                      "tarcstate": self.db.getTimeStamp()})
                        else:
                            # Job has not yet reached info system
                            self.log.warning("%s: Job %s is not yet in info system so cannot be cancelled" % (appjobid, job.JobID))
                    else:
                        self.log.error("%s: Could not cancel job %s" % (appjobid, job.JobID))
                        # Just to mark as cancelled so it can be cleaned
                        self.db.updateArcJob(id, {"arcstate": "cancelled",
                                                  "tarcstate": self.db.getTimeStamp()})
                else:
                    self.db.updateArcJob(id, {"arcstate": "cancelling",
                                              "tarcstate": self.db.getTimeStamp()})

    def processToResubmit(self):

        if self.cluster:
            jobstoresubmit = self.db.getArcJobs("arcstate='toresubmit' and cluster='"+self.cluster+"'")
        else:
            jobstoresubmit = self.db.getArcJobs("arcstate='toresubmit' and clusterlist=''")

        for proxyid, jobs in jobstoresubmit.items():
            self.uc.CredentialString(str(self.db.getProxy(proxyid)))

            # Clean up jobs which were submitted
            jobstoclean = [job[2] for job in jobs if job[2].JobID]

            if jobstoclean:

                # Put all jobs to cancel, however the supervisor will only cancel
                # cancellable jobs and remove the rest so there has to be 2 calls
                # to Clean()
                job_supervisor = arc.JobSupervisor(self.uc, jobstoclean)
                job_supervisor.Update()
                self.log.info("Cancelling %i jobs" % len(jobstoclean))
                job_supervisor.Cancel()

                processed = job_supervisor.GetIDsProcessed()
                notprocessed = job_supervisor.GetIDsNotProcessed()
                # Clean the successfully cancelled jobs
                if processed:
                    job_supervisor.SelectByID(processed)
                    self.log.info("Cleaning %i jobs" % len(processed))
                    if not job_supervisor.Clean():
                        self.log.warning("Failed to clean some jobs")

                # New job supervisor with the uncancellable jobs
                if notprocessed:
                    notcancellable = [job for job in jobstoclean if job.JobID in notprocessed]
                    job_supervisor = arc.JobSupervisor(self.uc, notcancellable)
                    job_supervisor.Update()

                    self.log.info("Cleaning %i jobs" % len(notcancellable))
                    if not job_supervisor.Clean():
                        self.log.warning("Failed to clean some jobs")

            # Empty job to reset DB info
            j = arc.Job()
            for (id, appjobid, job, created) in jobs:
                self.db.updateArcJob(id, {"arcstate": "tosubmit",
                                          "tarcstate": self.db.getTimeStamp(),
                                          "cluster": None}, j)

    def processToRerun(self):

        if not self.cluster:
            # Rerun only applies to job which have been submitted
            return

        jobstorerun = self.db.getArcJobs("arcstate='torerun' and cluster='"+self.cluster+"'")
        if not jobstorerun:
            return

        # TODO: downtimes from AGIS
        if self.conf.get(['downtime', 'srmdown']) == 'True':
            self.log.info('SRM down, not rerunning')
            return

        self.log.info("Resuming %i jobs" % sum(len(v) for v in jobstorerun.values()))
        for proxyid, jobs in jobstorerun.items():
            self.uc.CredentialString(str(self.db.getProxy(proxyid)))

            job_supervisor = arc.JobSupervisor(self.uc, [j[2] for j in jobs])
            job_supervisor.Update()
            # Renew proxy to be safe
            job_supervisor.Renew()
            job_supervisor = arc.JobSupervisor(self.uc, [j[2] for j in jobs])
            job_supervisor.Update()
            job_supervisor.Resume()

            notresumed = job_supervisor.GetIDsNotProcessed()

            for (id, appjobid, job, created) in jobs:
                if job.JobID in notresumed:
                    self.log.error("%s: Could not resume job %s" % (appjobid, job.JobID))
                    self.db.updateArcJob(id, {"arcstate": "failed",
                                              "tarcstate": self.db.getTimeStamp()})
                else:
                    # Force a wait before next status check, to allow the
                    # infosys to update and avoid the failed state being picked
                    # up again
                    self.db.updateArcJob(id, {"arcstate": "submitted",
                                              "tarcstate": self.db.getTimeStamp(time.time()+3600)})


    def process(self):

        # check jobs which failed to submit the previous loop
        self.checkFailedSubmissions()
        # process jobs which have to be cancelled
        self.processToCancel()
        # process jobs which have to be resubmitted
        self.processToResubmit()
        # process jobs which have to be rerun
        self.processToRerun()
        # submit new jobs
        while self.submit():
            continue


# Main
if __name__ == '__main__':
    asb=aCTSubmitter()
    asb.run()
    asb.finish()

