#!/usr/bin/env python3

import sys
from datetime import datetime
import os
import submitty_utils
import grade_items_logging
import grade_item
import fcntl
import glob
import multiprocessing
import time
import random
from queue import Empty
from watchdog.observers import Observer
from watchdog.events import FileCreatedEvent, FileSystemEventHandler


# ==================================================================================
# these variables will be replaced by INSTALL_SUBMITTY.sh
NUM_GRADING_SCHEDULER_WORKERS_string = "__INSTALL__FILLIN__NUM_GRADING_SCHEDULER_WORKERS__"
NUM_GRADING_SCHEDULER_WORKERS_int    = int(NUM_GRADING_SCHEDULER_WORKERS_string)

AUTOGRADING_LOG_PATH="__INSTALL__FILLIN__AUTOGRADING_LOG_PATH__"
SUBMITTY_DATA_DIR = "__INSTALL__FILLIN__SUBMITTY_DATA_DIR__"
HWCRON_UID = "__INSTALL__FILLIN__HWCRON_UID__"
INTERACTIVE_QUEUE = os.path.join(SUBMITTY_DATA_DIR, "to_be_graded_interactive")
BATCH_QUEUE = os.path.join(SUBMITTY_DATA_DIR, "to_be_graded_batch")


# ==================================================================================
class NewFileHandler(FileSystemEventHandler):
    """
    Simple handler for watchdog that watches for new files
    """

    def __init__(self, queue,new_job_event,overall_lock):
        super(FileSystemEventHandler, self).__init__()
        self.queue = queue
        self.new_job_event = new_job_event
        self.overall_lock = overall_lock

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            if os.path.basename(event.src_path).startswith("GRADING_") is False:
                # When a new queue file is created, add that job to
                # the queue and set the event that wakes up any
                # processes waiting on this event.
                self.overall_lock.acquire()
                self.queue.put(event.src_path)
                self.new_job_event.set()
                self.overall_lock.release()

# ==================================================================================
def initialize(untrusted_queue):
    """
    Initializer function for all our processes. We get one untrusted user off our queue which
    we then set in our Process. We cannot recycle the worker process as else the untrusted user
    we set for this process will be lost.

    :param untrusted_queue: multiprocessing.queues.Queue that contains all untrusted users left to
                            assign
    """
    multiprocessing.current_process().untrusted = untrusted_queue.get()


def grade_queue_file(queue_file,which_untrusted):
    """
    Grades a single item in one of the queues.

    :param queue_file: file path pointing to the file we want to operate one.
    """

    my_dir,my_file=os.path.split(queue_file)
    pid = os.getpid()
    directory = os.path.dirname(os.path.realpath(queue_file))
    name = os.path.basename(os.path.realpath(queue_file))
    grading_file = os.path.join(directory, "GRADING_" + name)

    open(os.path.join(grading_file), "w").close()
    #untrusted = multiprocessing.current_process().untrusted
    grade_item.just_grade_item(my_dir, queue_file, which_untrusted)

    # note: not necessary to acquire lock for these statements, but
    # make sure you remove the queue file, then the grading file
    os.remove(queue_file)
    os.remove(grading_file)


def populate_queue(queue, folder):
    """
    Populate a queue with all files in folder. We first scan the folder to check for any
    "GRADING_*" and clean them up, and then add the remaining files to the queue, sorted
    by creation time.

    :param queue: multiprocessing.queues.Queue
    :param folder: string representing the path to the folder to add files from
    """

    for file_path in glob.glob(os.path.join(folder, "GRADING_*")):
        grade_items_logging.log_message(False,"","","","","Remove old queue file: " + file_path)
        os.remove(file_path)

    # Grab all the files currently in the folder, sorted by creation
    # time, and put them in the queue to be graded
    files = glob.glob(os.path.join(folder, "*"))
    files.sort(key=os.path.getctime)
    for f in files:
        queue.put(os.path.join(folder, f))


# ==================================================================================
# ==================================================================================
def worker_process(interactive_queue,batch_queue,new_job_event,overall_lock,which_untrusted):
    """
    Each worker process spins in a loop, acquiring the overall lock to
    check the queues, prioritizing interactive jobs over batch jobs.
    If no jobs are available, the worker waits on an event editing one
    of the queues.
    """

    while True:
        overall_lock.acquire()
        if not interactive_queue.empty():
            job = interactive_queue.get()
            overall_lock.release()
            grade_queue_file(job,which_untrusted)
            continue
        elif not batch_queue.empty():
            job = batch_queue.get()
            overall_lock.release()
            grade_queue_file(job,which_untrusted)
            continue
        else:
            new_job_event.clear()
            overall_lock.release()
            pid = os.getpid()
            print ("pid ",pid,": no job for now, going to wait 10 seconds")
            new_job_event.wait(10)


# ==================================================================================
# ==================================================================================
def launch_workers(num_workers):

    # verify the hwcron user is running this script
    if not int(os.getuid()) == int(HWCRON_UID):
        raise SystemExit("ERROR: the grade_item.py script must be run by the hwcron user")

    grade_items_logging.log_message(False,"","","","","grade_scheduler.py launched")

    # prepare a list of untrusted users to be used by the workers
    untrusted_users = multiprocessing.Queue()
    for i in range(num_workers):
        untrusted_users.put("untrusted" + str(i).zfill(2))

    # Set up our queues that we're going to monitor for new jobs to run on
    interactive_queue = multiprocessing.Queue()
    batch_queue = multiprocessing.Queue()
    populate_queue(interactive_queue, INTERACTIVE_QUEUE)
    populate_queue(batch_queue, BATCH_QUEUE)

    # the workers will wait on event if the queues are exhausted
    new_job_event = multiprocessing.Event()

    # this lock will be used to edit the queue or new job event
    overall_lock = multiprocessing.Lock()

    # Setup watchdog observer that will watch the folders and run the handler on any
    # FileSystemEvents. This runs in a thread automatically.
    interactive_handler = NewFileHandler(interactive_queue,new_job_event,overall_lock)
    batch_handler = NewFileHandler(batch_queue,new_job_event,overall_lock)
    observer = Observer()
    observer.schedule(event_handler=interactive_handler, path=INTERACTIVE_QUEUE, recursive=False)
    observer.schedule(event_handler=batch_handler, path=BATCH_QUEUE, recursive=False)
    observer.start()

    # launch the worker threads
    processes = list()
    for i in range(0,num_workers):
        u = "untrusted" + str(i).zfill(2)
        p = multiprocessing.Process(target=worker_process,args=(interactive_queue,batch_queue,new_job_event,overall_lock,u))
        p.start()
        processes.append(p)

    # wait until the processes finish (doesn't happen under normal operation)
    for i in range(0,num_workers):
        processes[i].join()

    observer.stop()
    observer.join()

    grade_items_logging.log_message(False,"","","","","grade_scheduler.py terminated")


# ==================================================================================
if __name__ == "__main__":
    num_workers = NUM_GRADING_SCHEDULER_WORKERS_int
    launch_workers(num_workers)