import time
import logging
import multiprocessing

import rabix.common.six as six
from rabix import CONFIG
from rabix.common.util import import_name
from rabix.common.protocol import JobError
from rabix.runtime.tasks import Task
from rabix.runtime.jobs import Job

log = logging.getLogger(__name__)


class Engine(object):
    def __init__(self, before_task=None, after_task=None):
        self.jobs = {}
        self.before_task = before_task or (lambda t: None)
        self.after_task = after_task or (lambda t: None)

    __str__ = __unicode__ = __repr__ = lambda self: (
        '%s[%s jobs]' % (self.__class__.__name__, len(self.jobs))
    )

    def get_runner(self, task):
        runner_cfg = CONFIG['runners'][task.__class__.__name__]
        if isinstance(runner_cfg, six.string_types):
            return import_name(runner_cfg)(task)
        return import_name(runner_cfg[task.app.TYPE])(task)

    def run(self, *jobs):
        for job in jobs:
            self.jobs[job.job_id] = job
        self.run_all()

    def run_all(self):
        pass

    def _iter_ready(self):
        for job in six.itervalues(self.jobs):
            for task in job.tasks.get_ready_tasks():
                yield job, task

    def _update_jobs_check_ready(self):
        has_ready = False
        for job in six.itervalues(self.jobs):
            if job.tasks.get_ready_tasks():
                has_ready = True
                continue
            else:
                failed = any([
                    t.status != Task.FINISHED for t in job.tasks.iter_tasks()
                ])
                job.status = Job.FAILED if failed else Job.FINISHED
        return has_ready


class SequentialEngine(Engine):
    def run_task(self, task):
        task.status = Task.RUNNING
        try:
            task.result = self.get_runner(task).run()
            task.status = Task.FINISHED
        except Exception as e:
            log.exception('Task error (%s)', task.task_id)
            task.status = Task.FAILED
            task.result = e

    def run_all(self):
        for job in six.itervalues(self.jobs):
            if job.status != Job.QUEUED:
                continue
            job.status = Job.RUNNING
            try:
                self._run_job(job)
                job.status = Job.FINISHED
            except JobError as e:
                job.status = Job.FAILED
                job.error_message = str(e)

    def _run_job(self, job):
        log.info('Running job %s', job)
        ready = job.tasks.get_ready_tasks()
        while ready:
            for task in ready:
                self.before_task(task)
                self.run_task(task)
                if task.status == Task.FAILED:
                    raise JobError('Task %s failed. Reason: %s' %
                                   (task.task_id, task.result))
                self.after_task(task)
                job.tasks.resolve_task(task)
            ready = job.tasks.get_ready_tasks()


class MultiprocessingEngine(Engine):
    def __init__(self, before_task=None, after_task=None):
        super(MultiprocessingEngine, self).__init__(before_task, after_task)
        self.pool = multiprocessing.Pool()
        self.running = []
        self.total_ram = CONFIG['engine']['ram_mb']
        self.total_cpu = multiprocessing.cpu_count()
        self.available_ram = self.total_ram
        self.available_cpu = self.total_cpu
        self.multi_cpu_lock = False

    def _res(self):
        return '%s/%s%s;%s/%s' % (
            self.available_cpu,
            self.total_cpu, 'L' if self.multi_cpu_lock else '',
            self.available_ram,
            self.total_ram
        )

    def acquire_resources(self, task):
        res = task.resources
        log.debug('[resources: %s] Acquiring %s for %s',
                  self._res(), res, task)
        if res.mem_mb > self.available_ram:
            return False
        if (res.cpu == res.CPU_ALL and
                (self.multi_cpu_lock or self.available_cpu != self.total_cpu)):
            return False
        if res.cpu > self.available_cpu:
            return False
        self.available_ram -= res.mem_mb
        if res.cpu == res.CPU_ALL:
            self.multi_cpu_lock = True
        else:
            self.available_cpu -= res.cpu
        return True

    def release_resources(self, task):
        res = task.resources
        log.debug('[resources: %s] Releasing %s from %s',
                  self._res(), res, task)
        self.available_ram += res.mem_mb
        if res.cpu == res.CPU_ALL:
            self.multi_cpu_lock = False
        else:
            self.available_cpu += res.cpu

    def process_result(self, job, task, async_result):
        try:
            task.result = self.get_result_or_raise_error(async_result)
            task.status = Task.FINISHED
            log.info('Finished: %s', task)
            log.debug('Result: %s', task.result)
        except Exception as e:
            log.error('Failed: %s', task.task_id)
            task.status = Task.FAILED
            task.result = e
        self.after_task(task)
        self.release_resources(task)
        job.tasks.resolve_task(task)

    def _run_ready_tasks(self):
        for job, task in self._iter_ready():
            runner = self.get_runner(task)
            if not task.resources:
                task.resources = runner.get_requirements()
            if not self.acquire_resources(task):
                continue
            self.before_task(task)
            task.status = Task.RUNNING
            log.info('Running %s', task)
            log.debug('Arguments: %s', task.arguments)
            result = self.run_task_async(runner)
            self.running.append([job, task, result])

    def run_all(self):
        while True:
            self._run_ready_tasks()
            to_remove = []
            for ndx, item in enumerate(self.running):
                if self.check_async_result_ready(item[2]):
                    self.process_result(*item)
                    to_remove.append(ndx)
            if to_remove:
                self.running = [
                    x for n, x in enumerate(self.running) if n not in to_remove
                ]
                has_ready = self._update_jobs_check_ready()
                if not self.running and not has_ready:
                    return
            else:
                time.sleep(.1)

    def get_result_or_raise_error(self, async_result):
        return async_result.get()

    def run_task_async(self, runner):
        return self.pool.apply_async(runner)

    def check_async_result_ready(self, async_result):
        return async_result.ready()
