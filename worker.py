#!/usr/bin/env python2
# Render from a job server

import re
import os
import sys
import time
import uuid
import json
import socket
import itertools
from collections import namedtuple
from subprocess import check_call, check_output
from cStringIO import StringIO

import scipy
import redis

sys.path.insert(0, os.path.dirname(__file__))
from cuburn import render
from cuburn.genome import convert, db, use

import pycuda.driver as cuda

pycuda = None

# The default maximum number of waiting jobs. Also used to determine when a
# job has expired.
QUEUE_LENGTH=10

def partition(pred, arg):
    return filter(pred, arg), filter(lambda a: not pred(a), arg)

def git_rev():
    os.environ['GIT_DIR'] = os.path.join(os.path.dirname(__file__) or '.', '.git')
    if 'FLOCK_PATH_IGNORE' not in os.environ:
        if check_output('git status -z -uno'.split()):
            return None
    return check_output('git rev-parse HEAD'.split()).strip()[:10]

uu = lambda t: ':'.join((t, uuid.uuid1().hex))

def get_temperature():
    id = pycuda.autoinit.device.pci_bus_id()
    try:
        out = check_output('nvidia-smi -q -d TEMPERATURE'.split())
    except:
        return ''
    idx = out.find('\nGPU ' + id)
    if idx >= 0:
        out.find('Gpu', idx)
        if idx >= 0:
            idx = out.find(':')
            if idx >= 0:
                return out[idx+1:idx+3]
    return ''

def work(server):
    global pycuda
    import pycuda.autoinit
    rev = git_rev()
    assert rev, 'Repository must be clean!'
    r = redis.StrictRedis(server)
    wid = uu('workers')
    r.sadd('renderpool:' + rev + ':workers', wid)
    r.hmset(wid, {
        'host': socket.gethostname(),
        'devid': pycuda.autoinit.device.pci_bus_id(),
        'temp': get_temperature()
    })
    r.expire(wid, 180)
    last_ping = time.time()

    idx = evt = buf = None
    last_idx = last_buf = last_evt = two_evts_ago = None
    last_pid = last_gid = rdr = None

    mgr = render.RenderManager()

    while True:
        task = r.blpop('renderpool:' + rev + ':queue', 10)
        now = time.time()
        if now > last_ping - 60:
            r.hset(wid, 'temp', get_temperature())
            r.expire(wid, 180)
            last_ping = now

        # last_evt will be populated during normal queue operation (when evt
        # contains the most recent event), as well as when the render queue is
        # flushing due to not receiving a new task before the timeout.
        if last_idx is not None:
            while not last_evt.query():
                # This delay could probably be a lot higher with zero impact
                # on throughput for Fermi cards
                time.sleep(0.05)

            sid, sidx, ftag = last_idx
            obuf = StringIO()
            rdr.out.save(last_buf, obuf, 'jpeg')
            obuf.seek(0)
            gpu_time = last_evt.time_since(two_evts_ago)
            head = ' '.join([sidx, str(gpu_time), ftag])
            r.rpush(sid + ':queue', head + '\0' + obuf.read())
            print 'Pushed frame: %s' % head

        two_evts_ago, last_evt = last_evt, evt
        last_idx, last_buf = idx, buf

        if not task:
            idx = evt = buf = None
            continue

        copy = False
        sid, sidx, pid, gid, ftime, ftag = task[1].split(' ', 5)
        if pid != last_pid or gid != last_gid or not rdr:
            gnm = json.loads(r.get(gid))
            gprof, ignored_times = use.wrap_genome(json.loads(r.get(pid)), gnm)
            rdr = render.Renderer(gnm, gprof)
            last_pid, last_gid = pid, gid
            copy = True

        if last_evt is None:
            # Create a dummy event for timing
            last_evt = cuda.Event().record(mgr.stream_a)

        evt, buf = mgr.queue_frame(rdr, gnm, gprof, float(ftime), copy)
        idx = sid, sidx, ftag

def iter_genomes(prof, gpaths, pname='540p'):
    """
    Walk a list of genome paths, yielding them in an order suitable for
    the `genomes` argument of `create_jobs()`.
    """
    gdb = db.connect('.')

    for gpath in gpaths:
        gname = os.path.basename(gpath).rsplit('.', 1)[0]
        odir = 'out/%s/%s' % (pname, gname)
        if os.path.isfile(os.path.join(odir, 'COMPLETE')):
            continue
        with open(gpath) as fp:
            gsrc = fp.read()
        gnm = convert.edge_to_anim(gdb, json.loads(gsrc))
        gsrc = json.dumps(gnm)
        gprof, times = use.wrap_genome(prof, gnm)
        gtimes = []
        for i, t in enumerate(times):
            opath = os.path.join(odir, '%05d.jpg' % (i+1))
            if not os.path.isfile(opath):
                gtimes.append((t, opath))
        if gtimes:
            if not os.path.isdir(odir):
                os.makedirs(odir)
            with open(os.path.join(odir, 'NFRAMES'), 'w') as fp:
                fp.write(str(len(times)) + '\n')
            yield gsrc, gtimes

def create_jobs(r, psrc, genomes):
    """Attention politicians: it really is this easy.

    `genomes` is an iterable of 2-tuples (gsrc, gframes), where `gframes` is an
    iterable of 2-tuples (ftime, fid).
    """
    pid = uu('profile')
    r.set(pid, psrc)
    for gsrc, gframes in genomes:
        # TODO: SHA-based? I guess that depends on whether we do precompilation
        # on the HTTP server which accepts job requests (and on whether the
        # grid remains homogeneous).
        gid = uu('genome')
        r.set(gid, gsrc)
        r.publish('precompile', gid)

        for ftime, fid in gframes:
            yield pid, gid, str(ftime), fid

def run_jobs(r, rev, jobs):
    # TODO: session properties
    sid = uu('session')
    qid = sid + ':queue'
    pending = {}    # sidx -> job, for any job currently in the queue
    waiting = []    # sidx of jobs in queue normally
    retry = []      # sidx of jobs in queue a second time

    def push(i, job):
        j = ' '.join((sid, str(i)) + job)
        r.rpush('renderpool:' + rev + ':queue', j)

    def pull(block):
        if block:
            ret = r.blpop(qid, 180)
            if ret is None:
                # TODO: better exception
                raise ValueError("Timeout")
            ret = ret[1]
        else:
            ret = r.lpop(qid)
            if ret is None: return
        tags, jpg = ret.split('\0', 1)
        sidx, gpu_time, ftag = tags.split(' ', 2)
        sidx, gpu_time = int(sidx), float(gpu_time)
        if sidx in waiting:
            waiting.remove(sidx)
        if sidx in retry:
            retry.remove(sidx)
        if sidx in pending:
            pending.pop(sidx)
        else:
            print 'Got two responses for %d' % sidx
        if retry and retry[0] < sidx - 4 * QUEUE_LENGTH:
            # TODO: ensure that this doesn't happen accidentally; raise an
            # appropriate exception when it does
            print "Double retry!"
        expired, waiting[:] = partition(lambda w: w < sidx - QUEUE_LENGTH,
                                        waiting)
        for i in expired:
            push(i, pending[i])
            retry.append(i)
        return sidx, gpu_time, ftag, jpg

    try:
        for sidx, job in enumerate(jobs):
            while len(pending) > QUEUE_LENGTH:
                yield pull(True)
            ret = pull(False)
            if ret:
                yield ret
            pending[sidx] = job
            waiting.append(sidx)
            push(sidx, job)
    except KeyboardInterrupt:
        print 'Interrupt received, flushing already-dispatched frames'

    while pending:
        print '%d...' % len(pending)
        yield pull(True)

def client(ppath, gpaths):
    rev = git_rev()
    assert rev, 'Repository must be clean!'
    r = redis.StrictRedis()
    if not r.scard('renderpool:' + rev + ':workers'):
        # TODO: expire workers when they disconnect
        print 'No workers available at local cuburn revision, exiting.'
        return

    with open(ppath) as fp:
        psrc = fp.read()
    prof = json.loads(psrc)
    pname = os.path.basename(ppath).rsplit('.', 1)[0]

    jobiter = create_jobs(r, psrc, iter_genomes(prof, gpaths, pname))
    for sidx, gpu_time, ftag, jpg in run_jobs(r, rev, jobiter):
        with open(ftag, 'w') as fp:
            fp.write(jpg)
        print 'Wrote %s (took %g msec)' % (ftag, gpu_time)

if __name__ == "__main__":
    if sys.argv[1] == 'work':
        work(sys.argv[2] if len(sys.argv) > 2 else 'localhost')
    else:
        client(sys.argv[1], sys.argv[2:])
