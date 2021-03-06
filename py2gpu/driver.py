from ctypes import c_int, c_long, c_float, c_void_p, c_double, c_short
import numpy
from numpy import ctypeslib
import os
import platform
import subprocess

from .grammar import _gpu_funcs, indent_source
from .utils import get_arg_type
from .build_module import LIBEXT

def import_driver():
    parent = os.path.dirname(__file__)
    return ctypeslib.load_library('_driver', parent)
_driver = import_driver()

emulating = c_int.in_dll(_driver, 'emulating')

_driver.drv_alloc.argtypes = [c_int]
_driver.drv_alloc.restype = c_void_p

_driver.drv_free.argtypes = [c_void_p]
_driver.drv_free.restype = c_int

_driver.drv_htod.argtypes = [c_void_p, ctypeslib.ndpointer(), c_int]
_driver.drv_htod.restype = c_int

_driver.drv_dtoh.argtypes = [ctypeslib.ndpointer(), c_void_p, c_int]
_driver.drv_dtoh.restype = c_int

class GPUError(Exception):
    pass

class GPUMemory(object):
    def __init__(self, data):
        self.data = data

    def __del__(self):
        if _driver.drv_free(self.data):
            raise GPUError('Freeing of GPU data failed!')

    @property
    def _as_parameter_(self):
        return self.data

def mem_alloc(size):
    data = _driver.drv_alloc(size)
    if data is None:
        raise GPUError('Memory allocation failed!')
    return GPUMemory(data)

def mem_alloc_like(data):
    return mem_alloc(data.nbytes)

def memcpy_htod(target, source):
    if _driver.drv_htod(target, source, source.nbytes):
        raise GPUError('Copying to GPU failed!')

def memcpy_dtoh(target, source):
    if _driver.drv_dtoh(target, source, target.nbytes):
        raise GPUError('Copying to host failed!')

def to_device(data):
    mem = mem_alloc_like(data)
    memcpy_htod(mem, data)
    return mem

def splay(dims, maxthreads=()):
    grid = []
    block = []
    dimcount = len(dims)
    if dimcount < 3:
        dims += (3 - dimcount) * (1,)
    for dim, size in enumerate(dims):
        if size == 1:
            cpus = threads = 1
        elif dim == 2:
            threads = size
            cpus = 1
        else:
            # TODO: Optimize by calculating register usage
            if maxthreads is not None and len(maxthreads) > dim and maxthreads[dim]:
                threads = maxthreads[dim]
            else:
                threads = 64 if dimcount == 1 else 16
                if (dimcount == 1 and size < 256) or (dimcount > 1 and size < 64):
                    threads //= 2
            # For 3d images we have to make one of the first two dimensions smaller
            if dim == 0:
                threads //= dims[2]
            cpus = (size + threads - 1) // threads
        grid.append(cpus)
        block.append(threads)
    return grid, block

_base_source = r'''
extern "C" {
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
void initgpucpu() {}
#else
#define EXPORT
#endif

%s

%s
}
'''.lstrip()

_caller = r'''
EXPORT void __caller__kernel_%(name)s(%(args)s, int __cpus0, int __cpus1, int __cpus2, int __threads0, int __threads1, int __threads2) {
    dim3 __cpu_count(__cpus0, __cpus1, __cpus2);
    dim3 __thread_count(__threads0, __threads1, __threads2);
    __kernel_%(name)s<<< __cpu_count, __thread_count >>>(%(callargs)s);
    cudaThreadSynchronize();
}
'''.lstrip()

_argtypes = {
    'P': c_void_p,
    'i': c_int,
    'l': c_long,
    'f': c_float,
    'd': c_double,
    'h': c_short,
}

class Function(object):
    def __init__(self, name, func):
        self.name = name
        self.func = func
        func.restype = None

    def prepare(self, argtypes):
        types = []
        for argtype in argtypes + 'iiiiii':
            types.append(_argtypes[argtype])
        self.func.argtypes = types

    def __call__(self, cpu_count, thread_count, *args):
        args = list(args)
        args.extend(cpu_count + thread_count)
        return self.func(*args)

class SourceModule(object):
    def __init__(self, source, options=[]):
        callers = []
        for name, info in _gpu_funcs.items():
            if 'return' in info['types']:
                continue
            args = [arg.id for arg in info['funcnode'].args.args]
            types = info['types']
            funcargs = []
            callargs = []
            arrays = []
            for arg in args:
                kind = types[arg][1]
                if kind.endswith('Array'):
                    funcargs.append('%s *%s' % (get_arg_type(types[arg][2].type)[1], arg))
                    funcargs.extend('int32 __%s_shape%d' % (arg, dim) for dim in range(3))
                    callargs.append(arg)
                    callargs.extend('__%s_shape%d' % (arg, dim) for dim in range(3))
                else:
                    funcargs.append('%s %s' % (kind, arg))
                    callargs.append(arg)

            data = {
                'args': ', '.join(funcargs),
                'callargs': ', '.join(callargs),
                'name': name,
                'arrays': '\n'.join(arrays),
            }
            callers.append(_caller % data)
        self.source = _base_source % (source, '\n'.join(callers))
        options = options[:]
        if emulating:
            options.extend(['-deviceemu'])
            self.source = '#define DEVICEEMU 1\n' + self.source
        try:
            fp = open('gpucode.cu', 'r')
            changed = fp.read() != self.source
            fp.close()
        except IOError:
            changed = True
        if changed:
            fp = open('gpucode.cu', 'w')
            fp.write(self.source)
            fp.close()
            options.extend(['--shared', '--keep', '-O3', '-o', 'gpucode'+LIBEXT])
            if platform.system() != 'Windows' and platform.architecture()[0] == '64bit':
                options.extend(['--compiler-options', '-fPIC'])
            if subprocess.call(['nvcc'] + options + ['gpucode.cu']):
                raise ValueError('Could not compile GPU/CPU code')
        self.lib = ctypeslib.load_library('gpucode', '.')

    def get_function(self, name):
        name = '__caller' + name
        return Function(name, self.lib[name])
