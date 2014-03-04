# -*- coding: utf-8 -*-
import sys
import zmq
import uuid
import time
import threading
from time import sleep
from datetime import datetime
from datetime import timedelta

try:
    import queue
except ImportError:
    import Queue as queue

from . import Task
from . import logger

from .results.backends import Dummy

from .exceptions import TaskNotFound
from .exceptions import MessageMalformed

__all__ = ['Worker']


class Worker(object):
    """
    :class:`carotte.Worker` register and wait task to run.

    :param string bind: Address to bind
    :param int thread: Number of thread
    :param task_result_backend: :class:`carotte.results.backends.Base`
    :param task_result_expires: :class:`datetime.timedelta`

    >>> from carotte import Worker
    >>> worker = Worker(thread=10)
    >>> worker.add_task('hello', lambda: 'hello world')
    >>> worker.run()

    """
    def __init__(
            self, bind="tcp://127.0.0.1:5550", thread=5,
            task_result_backend=None, task_result_expires=None):
        self.thread = thread
        self.bind = bind
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.running = False
        self.tasks = {}

        if task_result_backend is None:
            task_result_backend = Dummy()
        self.task_result_backend = task_result_backend

        if task_result_expires is None:
            task_result_expires = timedelta(days=1)
        assert isinstance(task_result_expires, timedelta)
        self.task_result_expires = task_result_expires

        self.lock = threading.Lock()

        self.queue = queue.Queue()
        logger.info('Running %s thread(s)...' % self.thread)
        for i in range(int(self.thread)):
            t = threading.Thread(target=self._worker)
            t.daemon = True
            t.start()

    def cleanup_task_results(self):
        time_wait = 0
        time_step = 2
        while self.running:
            if time_wait >= self.task_result_expires.total_seconds():
                stats = self.task_result_backend.cleanup(self.task_result_expires)
                logger.info('Cleanup_results stats: %s' % stats)
                time_wait = 0
            sleep(time_step)
            time_wait += time_step

    def post_run(self):
        # Add cleanup results task
        t = threading.Thread(target=self.cleanup_task_results)
        t.start()

    def run(self):
        """
        Blocking method that run the server.
        """
        if self.tasks:
            logger.info('Registered tasks: %s' % ', '.join(self.tasks))
        else:
            logger.info('No tasks registered')
        logger.info('Listening on %s ...' % self.bind)
        self.socket.bind(self.bind)

        self.running = True
        self.post_run()

        while True:
            msg = self.socket.recv_pyobj()

            action = msg.get('action')
            if action == 'run_task':
                if msg.get('name') not in self.tasks:
                    response = {
                        'success': False, 'exception': TaskNotFound(msg.get('name'))}
                    self.socket.send_pyobj(response)
                else:
                    task = Task(str(uuid.uuid4()), msg.get('name'), [], {})

                    if msg.get('args'):
                        task.args = msg.get('args', [])
                    if msg.get('kwargs'):
                        task.kwargs = msg.get('kwargs', {})

                    self.task_result_backend.add_task(task)
                    self.queue.put(task.id)
                    self.socket.send_pyobj({'success': True, 'task': task})
            elif action == 'get_result':
                task_id = msg.get('id')
                task = self.task_result_backend.get_task(task_id)
                if task:
                    response = {'success': True, 'task': task}
                else:
                    response = {
                        'success': False,
                        'id': task_id,
                        'exception': TaskNotFound(task_id)}
                self.socket.send_pyobj(response)
            elif action == 'wait':
                task_id = msg.get('id')
                task = self.task_result_backend.get_task(task_id)
                if task:
                    while not task.terminated:
                        task = self.task_result_backend.get_task(task_id)
                        time.sleep(1)
                    response = {'success': True, 'task': task}
                else:
                    response = {
                        'success': False,
                        'id': task_id,
                        'exception': TaskNotFound(task_id)}
                self.socket.send_pyobj(response)
            else:
                response = {'success': False, 'exception': MessageMalformed()}
                self.socket.send_pyobj(response)

    def add_task(self, task_name, task):
        """
        Register a task to server.

        :param string task_name: Task name used by clients
        :param method task: Method that will be called
        """
        self.tasks[task_name] = task

    def delete_task(self, task_name):
        """
        Unregister a task from server.

        :param string task_name: Task name
        """
        del(self.tasks[task_name])

    def _worker(self):
        while True:
            task_id = self.queue.get()
            task = self.task_result_backend.get_task(task_id)

            with self.lock:
                logger.info('Running %s (args:%s) (kwargs:%s)' % (
                    task.name, task.args, task.kwargs))

            task_func = self.tasks.get(task.name, None)

            try:
                task.set_result(task_func(*task.args, **task.kwargs))
                task.set_success(True)
            except Exception as err:
                task.set_success(False)
                task.set_exception("%s" % err)

            task.set_terminated(True)
            task.set_terminated_at(datetime.now())
            self.task_result_backend.update_task(task)
            logger.info('Finished task %s (success: %s)' % (
                task_id, task.success))
            logger.info(task.exception)
            self.queue.task_done()

    def stop(self):
        """
        Stop server and all its threads.
        """
        self.running = False
        logger.info('Waiting tasks to finish...')
        self.queue.join()
        self.socket.close()
        logger.info('Exiting...')
        sys.exit(0)
