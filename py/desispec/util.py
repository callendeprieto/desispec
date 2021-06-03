"""
Utility functions for desispec
"""
from __future__ import absolute_import, division, print_function

import os
import sys
import errno
import time
import collections
import numbers

import numpy as np

import subprocess as sp

from desiutil.log import get_logger, INFO


def runcmd(cmd, args=None, inputs=[], outputs=[], clobber=False):
    """
    Runs a command, checking for inputs and outputs

    Args:
        cmd : command string to run with subprocess.call()
        inputs : list of filename inputs that must exist before running
        outputs : list of output filenames that should be created
        clobber : if True, run even if outputs already exist

    Returns:
        error code from command or input/output checking; 0 is good

    TODO:
        Should it raise an exception instead?

    Notes:
        If any inputs are missing, don't run cmd.
        If outputs exist and have timestamps after all inputs, don't run cmd.

    """
    log = get_logger()
    #- Check that inputs exist
    err = 0
    input_time = 0  #- timestamp of latest input file
    for x in inputs:
        if not os.path.exists(x):
            log.error("missing input "+x)
            err = 1
        else:
            input_time = max(input_time, os.stat(x).st_mtime)

    if err > 0:
        return err

    #- Check if outputs already exist and that their timestamp is after
    #- the last input timestamp
    already_done = (not clobber) and (len(outputs) > 0)
    if not clobber:
        for x in outputs:
            if not os.path.exists(x):
                already_done = False
                break
            if len(inputs)>0 and os.stat(x).st_mtime < input_time:
                already_done = False
                break

    if already_done:
        log.info("SKIPPING: {}".format(cmd))
        return 0

    #- Green light to go; print input/output info
    #- Use log.level to decide verbosity, but avoid long prefixes
    log.info(time.asctime())
    log.info("RUNNING: {}".format(cmd))
    if log.level <= INFO:
        if len(inputs) > 0:
            print("  Inputs")
            for x in inputs:
                print("   ", x)
        if len(outputs) > 0:
            print("  Outputs")
            for x in outputs:
                print("   ", x)

    #- run command
    if isinstance(cmd, collections.abc.Callable):
        if args is None:
            return cmd()
        else:
            return cmd(*args)
    else:
        if args is None:
            err = sp.call(cmd, shell=True)
        else:
            raise ValueError("Don't provide args unless cmd is function")

    log.info(time.asctime())
    if err > 0:
        log.critical("FAILED err={} {}".format(err, cmd))
        return err

    #- Check for outputs
    err = 0
    for x in outputs:
        if not os.path.exists(x):
            log.error("missing output "+x)
            err = 2
    if err > 0:
        log.critical("FAILED outputs {}".format(cmd))
        return err

    log.info("SUCCESS: {}".format(cmd))
    return 0

def mpi_count_failures(num_cmd, num_err, comm=None):
    """
    Sum num_cmd and num_err across MPI ranks

    Args:
        num_cmd (int): number of commands run
        num_err (int): number of failures

    Options:
        comm: mpi4py communicator

    Returns:
        sum(num_cmd), sum(num_err) summed across all MPI ranks

    If ``comm`` is None, returns input num_cmd, num_err
    """
    if comm is None:
        return num_cmd, num_err

    rank = comm.rank
    size = comm.size

    if num_cmd is None:
        num_cmd = 0
    if num_err is None:
        num_err = 0

    num_cmd_all = np.sum(comm.gather(num_cmd, root=0))
    num_err_all = np.sum(comm.gather(num_err, root=0))

    num_cmd_all = comm.bcast(num_cmd_all, root=0)
    num_err_all = comm.bcast(num_err_all, root=0)
    return num_cmd_all, num_err_all


def sprun(com, capture=False, input=None):
    """Run a command with subprocess and handle errors.

    This runs a command and returns the lines of STDOUT as a list.
    Any contents of STDERR are logged.  If an OSError is raised by
    the child process, that is also logged.  If another exception is
    raised by the child process, the traceback from the child process
    is printed.

    Args:
        com (list): the command to run.
        capture (bool): if True, return the stdout contents.
        input (str): the string data (can include embedded newlines) to write
            to the STDIN of the child process.

    Returns:
        tuple(int, (list)): the return code and optionally the lines of STDOUT
            from the child process.

    """
    import traceback
    log = get_logger()
    stdin = None
    if input is not None:
        stdin = sp.PIPE
    out = None
    err = None
    ret = -1
    try:
        with sp.Popen(com, stdin=stdin, stdout=sp.PIPE, stderr=sp.PIPE, universal_newlines=True) as p:
            if input is None:
                out, err = p.communicate()
            else:
                out, err = p.communicate(input=input)
            for line in err.splitlines():
                log.info("STDERR: {}".format(line))
            ret = p.returncode
    except OSError as e:
        log.error("OSError: {}".format(e.errno))
        log.error("OSError: {}".format(e.strerror))
        log.error("OSError: {}".format(e.filename))
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        for line in lines:
            log.error("exception: {}".format(line))
    if capture:
        return ret, out.splitlines()
    else:
        for line in out.splitlines():
            print(line)
        return ret


def pid_exists( pid ):
    """Check whether pid exists in the current process table.

    **UNIX only.**  Should work the same as psutil.pid_exists().

    Args:
        pid (int): A process ID.

    Returns:
        pid_exists (bool): ``True`` if the process exists in the current process table.
    """
    if pid < 0:
        return False
    if pid == 0:
        # According to "man 2 kill" PID 0 refers to every process
        # in the process group of the calling process.
        # On certain systems 0 is a valid PID but we have no way
        # to know that in a portable fashion.
        raise ValueError('invalid PID 0')
    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.ESRCH:
            # ESRCH == No such process
            return False
        elif err.errno == errno.EPERM:
            # EPERM clearly means there's a process to deny access to
            return True
        else:
            # According to "man 2 kill" possible error values are
            # (EINVAL, EPERM, ESRCH)
            raise
    else:
        return True



def option_list(opts):
    """Convert key, value pairs into command-line options.

    Parameters
    ----------
    opts : dict-like
        Convert a dictionary into command-line options.

    Returns
    -------
    :class:`list`
        A list of command-line options.
    """
    optlist = []
    for key, val in opts.items():
        keystr = "--{}".format(key)
        if isinstance(val, bool):
            if val:
                optlist.append(keystr)
        else:
            optlist.append(keystr)
            if isinstance(val, float):
                optlist.append("{:.14e}".format(val))
            elif isinstance(val, (list, tuple)):
                optlist.extend(val)
            else:
                optlist.append("{}".format(val))
    return optlist


def mask32(mask):
    '''
    Return an input mask as unsigned 32-bit

    Raises ValueError if 64-bit input can't be cast to 32-bit without losing
    info (i.e. if it contains values > 2**32-1)
    '''
    if mask.dtype in (
        np.dtype('i4'),  np.dtype('u4'),
        np.dtype('>i4'), np.dtype('>u4'),
        np.dtype('<i4'), np.dtype('<u4'),
        ):
        if mask.dtype.isnative:
            return mask.view('u4')
        else:
            return mask.astype('u4')

    elif mask.dtype in (
        np.dtype('i8'),  np.dtype('u8'),
        np.dtype('>i8'), np.dtype('>u8'),
        np.dtype('<i8'), np.dtype('<u8'),
        ):
        if mask.dtype.isnative:
            mask64 = mask.view('u8')
        else:
            mask64 = mask.astype('i8')
        if np.any(mask64 > 2**32-1):
            raise ValueError("mask with values above 2**32-1 can't be cast to 32-bit")
        return np.asarray(mask, dtype='u4')

    elif mask.dtype in (
        np.dtype('bool'), np.dtype('bool8'),
        np.dtype('i2'),  np.dtype('u2'),
        np.dtype('>i2'), np.dtype('>u2'),
        np.dtype('<i2'), np.dtype('<u2'),
        np.dtype('i1'),  np.dtype('u1'),
        np.dtype('>i1'), np.dtype('>u1'),
        np.dtype('<i1'), np.dtype('<u1'),
        ):
        return np.asarray(mask, dtype='u4')
    else:
        raise ValueError("Can't cast dtype {} to unsigned 32-bit".format(mask.dtype))

def night2ymd(night):
    """
    parse night YEARMMDD string into tuple of integers (year, month, day)
    """
    assert isinstance(night, str), 'night is not a string'
    assert len(night) == 8, 'invalid YEARMMDD night string '+night

    year = int(night[0:4])
    month = int(night[4:6])
    day = int(night[6:8])
    if month < 1 or 12 < month:
        raise ValueError('YEARMMDD month should be 1-12, not {}'.format(month))
    if day < 1 or 31 < day:
        raise ValueError('YEARMMDD day should be 1-31, not {}'.format(day))

    return (year, month, day)

def ymd2night(year, month, day):
    """
    convert year, month, day integers into cannonical YEARMMDD night string
    """
    return "{:04d}{:02d}{:02d}".format(year, month, day)

def mjd2night(mjd):
    """
    Convert MJD to YEARMMDD int night of KPNO sunset
    """
    from astropy.time import Time
    night = int(Time(mjd - 7/24. - 12/24., format='mjd').strftime('%Y%m%d'))
    return night

def dateobs2night(dateobs):
    """
    Convert DATE-OBS ISO8601 UTC string to YEARMMDD int night of KPNO sunset
    """
    # use astropy to flexibily handle multiple valid ISO8601 variants
    from astropy.time import Time
    try:
        mjd = Time(dateobs).mjd
    except ValueError:
        #- only use optional dependency dateutil if needed;
        #- it can handle some ISO8601 timezone variants that astropy can't
        from dateutil.parser import isoparser
        mjd = Time(isoparser().isoparse(dateobs))

    return mjd2night(mjd)

def header2night(header):
    """
    Return YEARMMDD night from FITS header, handling common problems
    """
    try:
        return int(header['NIGHT'])
    except (KeyError, ValueError, TypeError): # i.e. missing, not int, or None
        pass

    try:
        return dateobs2night(header['DATE-OBS'])
    except (KeyError, ValueError, TypeError): # i.e. missing, not ISO, or None
        pass

    try:
        return mjd2night(header['MJD-OBS'])
    except (KeyError, ValueError, TypeError): # i.e. missing, not float, or None
        pass

    raise ValueError('Unable to derive YEARMMDD from header NIGHT,DATE-OBS,MJD')

def combine_ivar(ivar1, ivar2):
    """
    Returns the combined inverse variance of two inputs, making sure not to
    divide by 0 in the process.

    ivar1 and ivar2 may be scalar or ndarray but must have the same dimensions
    """
    iv1 = np.atleast_1d(ivar1)  #- handle list, tuple, ndarray, and scalar input
    iv2 = np.atleast_1d(ivar2)
    assert np.all(iv1 >= 0), 'ivar1 has negative elements'
    assert np.all(iv2 >= 0), 'ivar2 has negative elements'
    assert iv1.shape == iv2.shape, 'shape mismatch {} vs. {}'.format(iv1.shape, iv2.shape)
    ii = (iv1 > 0) & (iv2 > 0)
    ivar = np.zeros(iv1.shape)
    ivar[ii] = 1.0 / (1.0/iv1[ii] + 1.0/iv2[ii])

    #- Convert back to python float if input was scalar
    if isinstance(ivar1, (float, numbers.Integral)):
        return float(ivar)
    #- If input was 0-dim numpy array, convert back to 0-di
    elif ivar1.ndim == 0:
        return np.asarray(ivar[0])
    else:
        return ivar


_matplotlib_backend = None

def set_backend(backend='agg'):
    global _matplotlib_backend
    if _matplotlib_backend is None:
        _matplotlib_backend = backend
        import matplotlib
        matplotlib.use(_matplotlib_backend)
    return


def healpix_degrade_fixed(nside, pixel):
    """
    Degrade a NEST ordered healpix pixel with a fixed ratio.

    This degrades the pixel to a lower nside value that is
    fixed to half the healpix "factor".

    Args:
        nside (int): a valid NSIDE value.
        pixel (int): the NESTED pixel index.

    Returns (tuple):
        a tuple of ints, where the first value is the new
        NSIDE and the second value is the degraded pixel
        index.

    """
    factor = int(np.log2(nside))
    subfactor = factor // 2
    subnside = 2**subfactor
    subpixel = pixel >> (factor - subfactor)
    return (subnside, subpixel)


def parse_int_args(arg_string) :
    """
    Short func that parses a string containing a comma separated list of
    integers, which can include ":" or ".." or "-" labeled ranges

    Args:
        arg_string (str) : list of integers or integer ranges

    Returns (array 1-D):
        1D numpy array listing all of the integers given in the list,
        including enumerations of ranges given.

    Note: this follows python-style ranges, i,e, 1:5 or 1..5 returns 1, 2, 3, 4
    """
    if arg_string is None :
        return np.array([])
    else:
        arg_string = str(arg_string)

    if len(arg_string.strip(' \t'))==0:
        return np.array([])

    fibers=[]

    log = get_logger()
    for sub in arg_string.split(',') :
        sub = sub.replace(' ','')
        if sub.isdigit() :
            fibers.append(int(sub))
            continue

        match = False
        for symbol in [':','..','-']:
            if not match and symbol in sub:
                tmp = sub.split(symbol)
                if (len(tmp) == 2) and tmp[0].isdigit() and tmp[1].isdigit() :
                    match = True
                    for f in range(int(tmp[0]),int(tmp[1])) :
                        fibers.append(f)

        if not match:
            log.warning("parsing error. Didn't understand {}".format(sub))
            sys.exit(1)

    return np.array(fibers)

def parse_fibers(fiber_string) :
    """
    Short func that parses a string containing a comma separated list of
    integers, which can include ":" or ".." or "-" labeled ranges

    Args:
        fiber_string (str) : list of integers or integer ranges

    Returns (array 1-D):
        1D numpy array listing all of the integers given in the list,
        including enumerations of ranges given.

    Note: this follows python-style ranges, i,e, 1:5 or 1..5 returns 1, 2, 3, 4
    """
    return parse_int_args(fiber_string)
