"""analysis.py - Run pipelines on imagesets to produce measurements.

CellProfiler is distributed under the GNU General Public License.
See the accompanying file LICENSE for details.

Copyright (c) 2003-2009 Massachusetts Institute of Technology
Copyright (c) 2009-2012 Broad Institute
All rights reserved.

Please see the AUTHORS file for credits.

Website: http://www.cellprofiler.org
"""
from __future__ import with_statement

import subprocess
import multiprocessing
import logging
import threading
import Queue
import uuid
import numpy as np
import cStringIO as StringIO
import sys
import os
import os.path
import zmq
import gc
import collections

import cellprofiler
import cellprofiler.measurements as cpmeas
import cellprofiler.workspace as cpw
import cellprofiler.cpimage as cpimage
import cellprofiler.preferences as cpprefs
import subimager.client
from cellprofiler.utilities.zmqrequest import AnalysisRequest, Request, Reply, UpstreamExit
from cellprofiler.utilities.zmqrequest import register_analysis, cancel_analysis
from cellprofiler.utilities.zmqrequest import get_announcer_address

logger = logging.getLogger(__name__)

use_analysis = True

DEBUG = 'DEBUG'
ANNOUNCE_DONE = "DONE"

class Analysis(object):
    '''An Analysis is the application of a particular pipeline of modules to a
    set of images to produce measurements.

    Multiprocessing for analyses is handled by multiple layers of threads and
    processes, to keep the GUI responsive and simplify the code.  Threads and
    processes are organized as below.  Display/Interaction requests and
    Exceptions are sent directly to the pipeline listener.

    +------------------------------------------------+
    |           CellProfiler GUI/WX thread           |
    |                                                |
    +- Analysis() methods down,  Events/Requests up -+
    |                                                |
    |       AnalysisRunner.interface() thread        |
    |                                                |
    +----------------  Queues  ----------------------+
    |                                                |
    |  AnalysisRunner.jobserver()/announce() threads |
    |                                                |
    +----------------------------------------------- +
    |              zmqrequest.Boundary()             |
    +---------------+----------------+---------------+
    |     Worker    |     Worker     |   Worker      |
    +---------------+----------------+---------------+

    Workers are managed by class variables in the AnalysisRunner.
    '''

    def __init__(self, pipeline, measurements_filename,
                 initial_measurements=None):
        '''create an Analysis applying pipeline to a set of images, writing out
        to measurements_filename, optionally starting with previous
        measurements.'''
        self.pipeline = pipeline
        initial_measurements = cpmeas.Measurements(copy=initial_measurements)
        self.initial_measurements_buf = initial_measurements.file_contents()
        initial_measurements.close()
        self.output_path = measurements_filename
        self.debug_mode = False
        self.analysis_in_progress = False
        self.runner = None

        self.runner_lock = threading.Lock()  # defensive coding purposes

    def start(self, analysis_event_callback, num_workers = None):
        with self.runner_lock:
            assert not self.analysis_in_progress
            self.analysis_in_progress = uuid.uuid1().hex

            self.runner = AnalysisRunner(self.analysis_in_progress,
                                         self.pipeline,
                                         self.initial_measurements_buf,
                                         self.output_path,
                                         analysis_event_callback)
            self.runner.start(num_workers)
            return self.analysis_in_progress

    def pause(self):
        with self.runner_lock:
            assert self.analysis_in_progress
            self.runner.pause()

    def resume(self):
        with self.runner_lock:
            assert self.analysis_in_progress
            self.runner.resume()

    def cancel(self):
        with self.runner_lock:
            if not self.analysis_in_progress:
                return
            self.analysis_in_progress = False
            self.runner.cancel()
            self.runner = None

    def check_running(self):
        '''Verify that an analysis is running, allowing the GUI to recover even
        if the AnalysisRunner fails in some way.

        Returns True if analysis is still running (threads are still alive).
        '''
        with self.runner_lock:
            if self.analysis_in_progress:
                return self.runner.check()
            return False


class AnalysisRunner(object):
    '''The AnalysisRunner manages two threads (per instance) and all of the
    workers (per class, i.e., singletons).

    The two threads run interface() and jobserver(), below.

    interface() is responsible grouping jobs for dispatch, tracking job
    progress, integrating measurements returned from workers.

    jobserver() is a lightweight thread that serves jobs and receives their
    requests, acting as a switchboard between workers, interface(), and
    whatever event_listener is present (via post_event()).

    workers are stored in AnalysisRunner.workers[], and are separate processes.
    They are expected to exit if their stdin() closes, e.g., if the parent
    process goes away.

    interface() and jobserver() communicate via Queues and using condition
    variables to get each other's attention.  zmqrequest is used to communicate
    between jobserver() and workers[].
    '''

    # worker pool - shared by all instances
    workers = []
    deadman_switches = []

    # measurement status
    STATUS = "ProcessingStatus"
    STATUS_UNPROCESSED = "Unprocessed"
    STATUS_IN_PROCESS = "InProcess"
    STATUS_FINISHED_WAITING = "FinishedWaitingMeasurements"
    STATUS_DONE = "Done"
    STATUSES = [STATUS_UNPROCESSED, STATUS_IN_PROCESS, STATUS_FINISHED_WAITING, STATUS_DONE]

    def __init__(self, analysis_id, pipeline,
                 initial_measurements_buf, 
                 output_path, event_listener):
        self.initial_measurements_buf = initial_measurements_buf

        # for writing results
        self.output_path = output_path

        self.analysis_id = analysis_id
        self.pipeline = pipeline.copy()
        self.event_listener = event_listener

        self.interface_work_cv = threading.Condition()
        self.jobserver_work_cv = threading.Condition()
        self.paused = False
        self.cancelled = False

        self.work_queue = Queue.Queue()
        self.in_process_queue = Queue.Queue()
        self.finished_queue = Queue.Queue()

        # We use a queue size of 10 because we keep measurements in memory (as
        # their HDF5 file contents) until they get merged into the full
        # measurements set.  If at some point, this size is too limiting, we
        # should have jobserver() call load_measurements_from_buffer() rather
        # than interface() doing so.  Currently, passing measurements in this
        # way seems like it might be buggy:
        # http://code.google.com/p/h5py/issues/detail?id=244
        self.received_measurements_queue = Queue.Queue(maxsize=10)

        self.shared_dicts = None

        self.interface_thread = None
        self.jobserver_thread = None

    # External control interfaces
    def start(self, num_workers = None):
        '''start the analysis run'''

        # Although it would be nice to reuse the worker pool, I'm not entirely
        # sure they recover correctly from the user cancelling an analysis
        # (e.g., during an interaction request).  This should be handled by
        # zmqRequest.cancel_analysis, but just in case, we stop the workers and
        # restart them.  Note that this creates a new announce port, so we
        # don't have to worry about old workers taking a job before noticing
        # that their stdin has closed.
        self.stop_workers()

        start_signal = threading.Semaphore(0)
        self.interface_thread = start_daemon_thread(
            target=self.interface, 
            args=(start_signal,),
            name='AnalysisRunner.interface')
        #
        # Wait for signal on interface started.
        #
        start_signal.acquire()
        self.jobserver_thread = start_daemon_thread(
            target=self.jobserver, 
            args=(self.analysis_id, start_signal), 
            name='AnalysisRunner.jobserver')
        #
        # Wait for signal on jobserver started.
        #
        start_signal.acquire()
        # start worker pool via class method (below)        
        self.start_workers(num_workers)

    def check(self):
        return ((self.interface_thread is not None) and
                (self.jobserver_thread is not None) and
                self.interface_thread.is_alive() and
                self.jobserver_thread.is_alive())

    def notify_threads(self):
        with self.interface_work_cv:
            self.interface_work_cv.notify()
        with self.jobserver_work_cv:
            self.jobserver_work_cv.notify()

    def cancel(self):
        '''cancel the analysis run'''
        logger.debug("Stopping workers")
        self.stop_workers()
        logger.debug("Canceling run")
        self.cancelled = True
        self.paused = False
        self.notify_threads()
        logger.debug("Waiting on interface thread")
        self.interface_thread.join()
        logger.debug("Waiting on jobserver thread")
        self.jobserver_thread.join()
        logger.debug("Cancel complete")

    def pause(self):
        '''pause the analysis run'''
        self.paused = True
        self.notify_threads()

    def resume(self):
        '''resume a paused analysis run'''
        self.paused = False
        self.notify_threads()

    # event posting
    def post_event(self, evt):
        self.event_listener(evt)

    # XXX - catch and deal with exceptions in interface() and jobserver() threads
    def interface(self, 
                  start_signal,
                  image_set_start=1, 
                  image_set_end=None,
                  overwrite=True):
        '''Top-half thread for running an analysis.  Sets up grouping for jobs,
        deals with returned measurements, reports status periodically.

        start_signal- signal this semaphore when jobs are ready.
        image_set_start - beginning image set number to process
        image_set_end - last image set number to process
        overwrite - whether to recompute imagesets that already have data in initial_measurements.
        '''
        posted_analysis_started = False
        acknowledged_thread_start = False
        measurements = None
        workspace = None
        try:
            # listen for pipeline events, and pass them upstream
            self.pipeline.add_listener(lambda pipe, evt: self.post_event(evt))
            
            fd = open(self.output_path, "wb")
            fd.write(self.initial_measurements_buf)
            fd.close()
            measurements = cpmeas.Measurements(image_set_start=None,
                                               filename=self.output_path,
                                               mode="a")
            # The shared dicts are needed in jobserver()
            self.shared_dicts = [m.get_dictionary() for m in self.pipeline.modules()]
            workspace = cpw.Workspace(self.pipeline, None, None, None,
                                      measurements, cpimage.ImageSetList())
    
            if image_set_end is None:
                image_set_end = measurements.get_image_numbers()[-1]
            image_sets_to_process = filter(
                lambda x: x >= image_set_start and x <= image_set_end,
                measurements.get_image_numbers())

            self.post_event(AnalysisStarted())
            posted_analysis_started = True

            # reset the status of every image set that needs to be processed
            for image_set_number in image_sets_to_process:
                if (overwrite or
                    (not measurements.has_measurements(cpmeas.IMAGE, self.STATUS, image_set_number)) or
                    (measurements[cpmeas.IMAGE, self.STATUS, image_set_number] != self.STATUS_DONE)):
                    measurements[cpmeas.IMAGE, self.STATUS, image_set_number] = self.STATUS_UNPROCESSED

            # Find image groups.  These are written into measurements prior to
            # analysis.  Groups are processed as a single job.
            if measurements.has_groups():
                worker_runs_post_group = True
                job_groups = {}
                for image_set_number in image_sets_to_process:
                    group_number = measurements[cpmeas.IMAGE, 
                                                cpmeas.GROUP_NUMBER, 
                                                image_set_number]
                    group_index = measurements[cpmeas.IMAGE, 
                                               cpmeas.GROUP_INDEX, 
                                               image_set_number]
                    job_groups[group_number] = job_groups.get(group_number, []) + [(group_index, image_set_number)]
                job_groups = [[isn for _, isn in sorted(job_groups[group_number])] 
                              for group_number in sorted(job_groups)]
            else:
                worker_runs_post_group = False  # prepare_group will be run in worker, but post_group is below.
                job_groups = [[image_set_number] for image_set_number in image_sets_to_process]

            # XXX - check that any constructed groups are complete, i.e.,
            # image_set_start and image_set_end shouldn't carve them up.

            # put the first job in the queue, then wait for the first image to
            # finish (see the check of self.finish_queue below) to post the rest.
            # This ensures that any shared data from the first imageset is
            # available to later imagesets.
            self.work_queue.put((job_groups[0], 
                                 worker_runs_post_group,
                                 True))
            start_signal.release()
            acknowledged_thread_start = True
            del job_groups[0]

            waiting_for_first_imageset = True

            # We loop until every image is completed, or an outside event breaks the loop.
            while not self.cancelled:

                # gather measurements
                while not self.received_measurements_queue.empty():
                    job, buf = self.received_measurements_queue.get()
                    recd_measurements = cpmeas.load_measurements_from_buffer(buf)
                    for object in recd_measurements.get_object_names():
                        if object == cpmeas.EXPERIMENT:
                            continue  # Written during prepare_run / post_run
                        for feature in recd_measurements.get_feature_names(object):
                            for imagenumber in job:
                                measurements[object, feature, imagenumber] \
                                    = recd_measurements[object, feature, imagenumber]
                    for image_set_number in job:
                        measurements[cpmeas.IMAGE, self.STATUS, int(image_set_number)] = self.STATUS_DONE
                    recd_measurements.close()
                    del recd_measurements

                # check for jobs in progress
                while not self.in_process_queue.empty():
                    image_set_numbers = self.in_process_queue.get()
                    for image_set_number in image_set_numbers:
                        measurements[cpmeas.IMAGE, self.STATUS, int(image_set_number)] = self.STATUS_IN_PROCESS

                # check for finished jobs that haven't returned measurements, yet
                while not self.finished_queue.empty():
                    finished_req = self.finished_queue.get()
                    measurements[cpmeas.IMAGE, self.STATUS, int(finished_req.image_set_number)] = self.STATUS_FINISHED_WAITING
                    if waiting_for_first_imageset:
                        assert isinstance(finished_req, 
                                          ImageSetSuccessWithDictionary)
                        self.shared_dicts = finished_req.shared_dicts
                        waiting_for_first_imageset = False
                        assert len(self.shared_dicts) == len(self.pipeline.modules())
                        # if we had jobs waiting for the first image set to finish,
                        # queue them now that the shared state is available.
                        for job in job_groups:
                            self.work_queue.put((job, worker_runs_post_group, False))
                    finished_req.reply(Ack())

                # check progress and report
                counts = collections.Counter(measurements[cpmeas.IMAGE, self.STATUS, image_set_number]
                                             for image_set_number in image_sets_to_process)
                self.post_event(AnalysisProgress(counts))

                # Are we finished?
                if counts[self.STATUS_DONE] == len(image_sets_to_process):
                    last_image_number = measurements.get_image_numbers()[-1]
                    measurements.image_set_number = last_image_number
                    if not worker_runs_post_group:
                        self.pipeline.post_group(workspace, {})
                    # XXX - revise pipeline.post_run to use the workspace
                    self.pipeline.post_run(measurements, None, None)
                    break

                measurements.flush()
                # not done, wait for more work
                with self.interface_work_cv:
                    while (self.paused or
                           ((not self.cancelled) and
                            self.in_process_queue.empty() and
                            self.finished_queue.empty() and
                            self.received_measurements_queue.empty())):
                        self.interface_work_cv.wait()  # wait for a change of status or work to arrive
        finally:
            # Note - the measurements file is owned by the queue consumer
            #        after this post_event unless the analysis was cancelled.
            #        If cancelled, the consumer never sees it and we
            #        close it immediately. If it is temporary, it will be
            #        deleted, otherwise, it will have partially complete
            #        but possibly useful measurements.
            #
            if not acknowledged_thread_start:
                start_signal.release()
            if posted_analysis_started:
                was_cancelled = self.cancelled
                self.post_event(AnalysisFinished(
                    None if was_cancelled else measurements, was_cancelled))
                if was_cancelled and measurements is not None:
                    if workspace is not None:
                        del workspace
                    measurements.close()
            self.stop_workers()
        self.analysis_id = False  # this will cause the jobserver thread to exit

    def jobserver(self, analysis_id, start_signal):
        # this server subthread should be very lightweight, as it has to handle
        # all the requests from workers, of which there might be several.

        # start the zmqrequest Boundary
        request_queue = Queue.Queue()
        boundary = register_analysis(analysis_id, 
                                     request_queue)
        #
        # The boundary is announcing our analysis at this point. Workers
        # will get announcements if they connect.
        #
        start_signal.release()

        # XXX - is this just to keep from posting another AnalysisPaused event?
        # If so, probably better to simplify the code and keep sending them
        # (should be only one per second).
        i_was_paused_before = False

        # start serving work until the analysis is done (or changed)
        while not self.cancelled:

            with self.jobserver_work_cv:
                if self.paused and not i_was_paused_before:
                    self.post_event(AnalysisPaused())
                    i_was_paused_before = True
                if self.paused or request_queue.empty():
                    self.jobserver_work_cv.wait(1)  # we timeout in order to keep announcing ourselves.
                    continue  # back to while... check that we're still running

            if i_was_paused_before:
                self.post_event(AnalysisResumed())
                i_was_paused_before = False

            try:
                req = request_queue.get(timeout=0.25)
            except Queue.Empty:
                continue
            
            if isinstance(req, PipelinePreferencesRequest):
                req.reply(Reply(pipeline_blob=np.array(self.pipeline_as_string()),
                                preferences=cpprefs.preferences_as_dict()))
            elif isinstance(req, InitialMeasurementsRequest):
                req.reply(Reply(buf=self.initial_measurements_buf))
            elif isinstance(req, WorkRequest):
                if not self.work_queue.empty():
                    job, worker_runs_post_group, wants_dictionary = \
                        self.work_queue.get()
                    req.reply(WorkReply(
                        image_set_numbers=job, 
                        worker_runs_post_group=worker_runs_post_group,
                        wants_dictionary = wants_dictionary))
                    self.queue_dispatched_job(job)
                else:
                    # there may be no work available, currently, but there
                    # may be some later.
                    req.reply(NoWorkReply())
            elif isinstance(req, ImageSetSuccess):
                # interface() is responsible for replying, to allow it to
                # request the shared_state dictionary if needed.
                self.queue_imageset_finished(req)
            elif isinstance(req, SharedDictionaryRequest):
                req.reply(SharedDictionaryReply(dictionaries=self.shared_dicts))
            elif isinstance(req, MeasurementsReport):
                self.queue_received_measurements(req.image_set_numbers,
                                                 req.buf)
                req.reply(Ack())
            elif isinstance(req, (InteractionRequest, DisplayRequest, 
                                  ExceptionReport, DebugWaiting, DebugComplete)):
                # bump upward
                self.post_event(req)
            else:
                raise ValueError("Unknown request from worker: %s of type %s" % (req, type(req)))

        # stop the ZMQ-boundary thread - will also deal with any requests waiting on replies
        boundary.cancel(analysis_id)

    def queue_job(self, image_set_number):
        self.work_queue.put(image_set_number)

    def queue_dispatched_job(self, job):
        self.in_process_queue.put(job)
        # notify interface thread
        with self.interface_work_cv:
            self.interface_work_cv.notify()

    def queue_imageset_finished(self, finished_req):
        self.finished_queue.put(finished_req)
        # notify interface thread
        with self.interface_work_cv:
            self.interface_work_cv.notify()

    def queue_received_measurements(self, image_set_numbers, measurements):
        self.received_measurements_queue.put((image_set_numbers, measurements))
        # notify interface thread
        with self.interface_work_cv:
            self.interface_work_cv.notify()

    def pipeline_as_string(self):
        s = StringIO.StringIO()
        self.pipeline.savetxt(s)
        return s.getvalue()

    # Class methods for managing the worker pool
    @classmethod
    def start_workers(cls, num=None):
        if cls.workers:
            return

        try:
            num = multiprocessing.cpu_count() if num is None else num
        except NotImplementedError:
            num = 4

        root_dir = os.path.abspath(
            os.path.join(os.path.dirname(cellprofiler.__file__), '..'))
        if 'PYTHONPATH' in os.environ:
            old_path = os.environ['PYTHONPATH']
            if not any([root_dir == path 
                        for path in old_path.split(os.pathsep)]):
                os.environ['PYTHONPATH'] =  root_dir + os.pathsep + old_path
        else:
            os.environ['PYTHONPATH'] = os.path.join(os.path.dirname(cellprofiler.__file__), '..')

        
        cls.work_announce_address = get_announcer_address()
        if 'CP_DEBUG_WORKER' in os.environ:
            logger.info("Announcing work at %s" % cls.work_announce_address)
            logger.info("Subimager port: %d" % subimager.client.port)
            logger.info("Please manually start a worker using the command-line:")
            logger.info(("python -u %s --work-announce %s "
                         "--subimager-port %d") % (
                             find_analysis_worker_source(),
                             cls.work_announce_address,
                             subimager.client.port))
            return
                
        # start workers
        for idx in range(num):
            # stdin for the subprocesses serves as a deadman's switch.  When
            # closed, the subprocess exits.
            if hasattr(sys, 'frozen') and sys.platform.startswith("win"):
                root_path = os.path.split(os.path.abspath(sys.argv[0]))[0]
                aw_path = os.path.join(root_path, "analysis_worker")
                worker = subprocess.Popen([aw_path,
                                           '--work-announce',
                                           cls.work_announce_address,
                                           '--subimager-port',
                                           '%d' % subimager.client.port],
                                          env=find_worker_env(),
                                          stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            else:
                worker = subprocess.Popen([find_python(),
                                           '-u',  # unbuffered
                                           find_analysis_worker_source(),
                                           '--work-announce',
                                           cls.work_announce_address,
                                           '--subimager-port',
                                           '%d' % subimager.client.port],
                                          env=find_worker_env(),
                                          stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)

            def run_logger(workR, widx):
                while(True):
                    try:
                        line = workR.stdout.readline()
                        if not line:
                            break
                        logger.info("Worker %d: %s", widx, line.rstrip())
                    except:
                        break
            start_daemon_thread(target=run_logger, args=(worker, idx,), name='worker stdout logger')

            cls.workers += [worker]
            cls.deadman_switches += [worker.stdin]  # closing stdin will kill subprocess

    @classmethod
    def stop_workers(cls):
        for deadman_switch in cls.deadman_switches:
            deadman_switch.close()
        for worker in cls.workers:
            worker.wait()
        cls.workers = []
        cls.deadman_swtiches = []


def find_python():
    if hasattr(sys, 'frozen'):
        if sys.platform == "darwin":
            app_python = os.path.join(os.path.dirname(os.environ['ARGVZERO']), "python")
            return app_python
    return 'python'


def find_worker_env():
    if hasattr(sys, 'frozen'):
        if sys.platform == "darwin":
            newenv = os.environ.copy()
            # http://mail.python.org/pipermail/pythonmac-sig/2005-April/013852.html
            newenv['PYTHONPATH'] = ':'.join([p for p in sys.path if isinstance(p, basestring)])
            return newenv
    return os.environ


def find_analysis_worker_source():
    # import here to break circular dependency.
    import cellprofiler.analysis  # used to get the path to the code
    return os.path.join(os.path.dirname(cellprofiler.analysis.__file__), "analysis_worker.py")


def start_daemon_thread(target=None, args=(), name=None):
    thread = threading.Thread(target=target, args=args, name=name)
    thread.daemon = True
    thread.start()
    return thread

###############################
# Request, Replies, Events
###############################
class AnalysisStarted(object):
    pass


class AnalysisProgress(object):
    def __init__(self, counts):
        self.counts = counts


class AnalysisPaused(object):
    pass


class AnalysisResumed(object):
    pass


class AnalysisFinished(object):
    def __init__(self, measurements, cancelled):
        self.measurements = measurements
        self.cancelled = cancelled


class PipelinePreferencesRequest(AnalysisRequest):
    pass


class InitialMeasurementsRequest(AnalysisRequest):
    pass


class WorkRequest(AnalysisRequest):
    pass


class ImageSetSuccess(AnalysisRequest):
    def __init__(self, analysis_id, image_set_number=None):
        AnalysisRequest.__init__(self, analysis_id, 
                                 image_set_number=image_set_number)
        
class ImageSetSuccessWithDictionary(ImageSetSuccess):
    def __init__(self, analysis_id, image_set_number, shared_dicts):
        ImageSetSuccess.__init__(self, analysis_id, 
                                 image_set_number=image_set_number)
        self.shared_dicts = shared_dicts


class DictionaryReqRep(Reply):
    pass



class MeasurementsReport(AnalysisRequest):
    def __init__(self, analysis_id, buf, image_set_numbers=[]):
        AnalysisRequest.__init__(self, analysis_id, 
                                 buf=buf, 
                                 image_set_numbers=image_set_numbers)


class InteractionRequest(AnalysisRequest):
    pass

class DisplayRequest(AnalysisRequest):
    pass

class SharedDictionaryRequest(AnalysisRequest):
    def __init__(self, analysis_id, module_num=-1):
        AnalysisRequest.__init__(self, analysis_id, module_num=module_num)


class SharedDictionaryReply(Reply):
    def __init__(self, dictionaries=[{}]):
        Reply.__init__(self, dictionaries=dictionaries)


class ExceptionReport(AnalysisRequest):
    def __init__(self, analysis_id,
                 image_set_number, module_name,
                 exc_type, exc_message, exc_traceback,
                 filename, line_number):
        AnalysisRequest.__init__(self,
                                 analysis_id,
                                 image_set_number=image_set_number,
                                 module_name=module_name,
                                 exc_type=exc_type,
                                 exc_message=exc_message,
                                 exc_traceback=exc_traceback,
                                 filename=filename,
                                 line_number=line_number)

    def __str__(self):
        return "(Worker) %s: %s"% (self.exc_type, self.exc_message)

class ExceptionPleaseDebugReply(Reply):
    def __init__(self, disposition, verification_hash=None):
        Reply.__init__(self, disposition=disposition, verification_hash=verification_hash)

class DebugWaiting(AnalysisRequest):
    '''Communicate the debug port to the server and wait for server OK to attach'''
    def __init__(self, analysis_id, port):
        AnalysisRequest.__init__(self, 
                                 analysis_id = analysis_id,
                                 port=port)
class DebugCancel(Reply):
    '''If sent in response to DebugWaiting, the user has changed his/her mind'''

class DebugComplete(AnalysisRequest):
    pass


class InteractionReply(Reply):
    pass


class WorkReply(Reply):
    pass


class NoWorkReply(Reply):
    pass


class ServerExited(UpstreamExit):
    pass


class Ack(Reply):
    def __init__(self, message="THANKS"):
        Reply.__init__(self, message=message)


if __name__ == '__main__':
    import time
    import cellprofiler.pipeline
    import cellprofiler.preferences
    import cellprofiler.utilities.thread_excepthook
    subimager.client.start_subimager()

    # This is an ugly hack, but it's necesary to unify the Request/Reply
    # classes above, so that regardless of whether this is the current module,
    # or a separately imported one, they see the same classes.
    import cellprofiler.analysis
    globals().update(cellprofiler.analysis.__dict__)

    print "TESTING", WorkRequest is cellprofiler.analysis.WorkRequest
    print id(WorkRequest), id(cellprofiler.analysis.WorkRequest)

    cellprofiler.utilities.thread_excepthook.install_thread_sys_excepthook()

    cellprofiler.preferences.set_headless()
    logging.root.setLevel(logging.INFO)
    logging.root.addHandler(logging.StreamHandler())

    batch_data = sys.argv[1]
    pipeline = cellprofiler.pipeline.Pipeline()
    pipeline.load(batch_data)
    measurements = cellprofiler.measurements.load_measurements(batch_data)
    analysis = Analysis(pipeline, 'test_out.h5', initial_measurements=measurements)

    keep_going = True

    def callback(event):
        global keep_going
        print "Pipeline Event", event
        if isinstance(event, AnalysisFinished):
            keep_going = False

    analysis.start(callback)
    while keep_going:
        time.sleep(0.25)
    del analysis
    subimager.client.stop_subimager()
    gc.collect()
