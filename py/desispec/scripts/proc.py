"""
One stop shopping for processing a DESI exposure

Examples at NERSC:

# ARC: 18 min on 2 nodes
time srun -N 2 -n 60 -C haswell -t 25:00 --qos realtime desi_proc --mpi -n 20191029 -e 22486

# FLAT: 13 min
time srun -n 20 -N 1 -C haswell -t 15:00 --qos realtime desi_proc --mpi -n 20191029 -e 22487

# TWILIGHT: 8min
time srun -n 20 -N 1 -C haswell -t 15:00 --qos realtime desi_proc --mpi -n 20191029 -e 22497

# SKY: 11 min
time srun -n 20 -N 1 -C haswell -t 15:00 --qos realtime desi_proc --mpi -n 20191029 -e 22536

# ZERO: 2 min
time srun -n 20 -N 1 -C haswell -t 15:00 --qos realtime desi_proc --mpi -n 20191029 -e 22561
"""

import time, datetime
start_imports = time.time()

#- enforce a batch-friendly matplotlib backend
from desispec.util import set_backend
set_backend()

import sys, os, argparse, re
import subprocess
from copy import deepcopy
import json

import numpy as np
import fitsio
from astropy.io import fits

from astropy.table import Table,vstack

import glob
import desiutil.timer
import desispec.io
from desispec.io import findfile, replace_prefix, shorten_filename
from desispec.io.util import create_camword, decode_camword, parse_cameras
from desispec.io.util import validate_badamps, get_tempfilename
from desispec.calibfinder import findcalibfile,CalibFinder,badfibers
from desispec.fiberflat import apply_fiberflat
from desispec.sky import subtract_sky
from desispec.util import runcmd
import desispec.scripts.assemble_fibermap
import desispec.scripts.preproc
import desispec.scripts.inspect_dark
import desispec.scripts.trace_shifts
import desispec.scripts.interpolate_fiber_psf
import desispec.scripts.extract
import desispec.scripts.badcolumn_mask
import desispec.scripts.specex
import desispec.scripts.fiberflat
import desispec.scripts.humidity_corrected_fiberflat
import desispec.scripts.sky
import desispec.scripts.stdstars
import desispec.scripts.select_calib_stars
import desispec.scripts.fluxcalibration
import desispec.scripts.procexp
import desispec.scripts.nightly_bias
from desispec.maskbits import ccdmask
from desispec.util import runcmd

from desitarget.targetmask import desi_mask

from desiutil.log import get_logger, DEBUG, INFO
import desiutil.iers

from desispec.workflow.desi_proc_funcs import assign_mpi, get_desi_proc_parser, update_args_with_headers, \
    find_most_recent
from desispec.workflow.desi_proc_funcs import determine_resources, create_desi_proc_batch_script

stop_imports = time.time()

#########################################
######## Begin Body of the Code #########
#########################################

def parse(options=None):
    parser = get_desi_proc_parser()
    args = parser.parse_args(options)
    return args

def _log_timer(timer, timingfile=None, comm=None):
    """
    Log timing info, optionally writing to json timingfile

    Args:
        timer: desiutil.timer.Timer object

    Options:
        timingfile (str): write json output to this file
        comm: MPI communicator

    If comm is not None, collect timers across ranks.
    If timmingfile already exists, read and append timing then re-write.
    """

    log = get_logger()
    if comm is not None:
        timers = comm.gather(timer, root=0)
        rank, size = comm.rank, comm.size
    else:
        timers = [timer,]
        rank, size = 0, 1

    if rank == 0:
        stats = desiutil.timer.compute_stats(timers)
        if timingfile:
            if os.path.exists(timingfile):
                with open(timingfile) as fx:
                    previous_stats = json.load(fx)

                #- augment previous_stats with new entries, but don't overwrite old
                for name in stats:
                    if name not in previous_stats:
                        previous_stats[name] = stats[name]

                stats = previous_stats

            tmpfile = get_tempfilename(timingfile)
            with open(tmpfile, 'w') as fx:
                json.dump(stats, fx, indent=2)
            os.rename(tmpfile, timingfile)
            log.info(f'Timing stats saved to {timingfile}')

        log.info('Timing max duration per step [seconds]:')
        for stepname, steptiming in stats.items():
            tmax = steptiming['duration.max']
            log.info(f'  {stepname:16s} {tmax:.2f}')


def main(args=None, comm=None):
    if not isinstance(args, argparse.Namespace):
        args = parse(args)

    log = get_logger()
    start_time = time.time()
    error_count = 0

    start_mpi_connect = time.time()
    if comm is not None:
        #- Use the provided comm to determine rank and size
        rank = comm.rank
        size = comm.size
    else:
        #- Check MPI flags and determine the comm, rank, and size given the arguments
        comm, rank, size = assign_mpi(do_mpi=args.mpi, do_batch=args.batch, log=log)
    stop_mpi_connect = time.time()

    if rank == 0:
        thisfile=os.path.dirname(os.path.abspath(__file__))
        thistime=datetime.datetime.fromtimestamp(start_imports).isoformat()
        log.info(f'rank 0 started {thisfile} at {thistime}')
    #- Start timer; only print log messages from rank 0 (others are silent)
    timer = desiutil.timer.Timer(silent=(rank>0))

    #- Fill in timing information for steps before we had the timer created
    if args.starttime is not None:
        timer.start('startup', starttime=args.starttime)
        timer.stop('startup', stoptime=start_imports)

    timer.start('imports', starttime=start_imports)
    timer.stop('imports', stoptime=stop_imports)

    timer.start('mpi_connect', starttime=start_mpi_connect)
    timer.stop('mpi_connect', stoptime=stop_mpi_connect)

    #- Freeze IERS after parsing args so that it doesn't bother if only --help
    timer.start('freeze_iers')
    desiutil.iers.freeze_iers()
    timer.stop('freeze_iers')

    #- Preflight checks
    timer.start('preflight')
    if rank > 0:
        #- Let rank 0 fetch these, and then broadcast
        args, hdr, camhdr = None, None, None
    else:
        if args.nightlybias and (args.expid is None) and (args.input is None):
            hdr = camhdr = None
        else:
            args, hdr, camhdr = update_args_with_headers(args)

    ## Make sure badamps is formatted properly
    if comm is not None and rank == 0 and args.badamps is not None:
        args.badamps = validate_badamps(args.badamps)

    if comm is not None:
        args = comm.bcast(args, root=0)
        hdr = comm.bcast(hdr, root=0)
        camhdr = comm.bcast(camhdr, root=0)

    known_obstype = ['SCIENCE', 'ARC', 'FLAT', 'ZERO', 'DARK',
        'TESTARC', 'TESTFLAT', 'PIXFLAT', 'SKY', 'TWILIGHT', 'OTHER']
    only_nightlybias = args.nightlybias and args.expid is None
    if args.obstype not in known_obstype and not only_nightlybias:
        raise ValueError('obstype {} not in {}'.format(args.obstype, known_obstype))

    if args.expid is None and not args.nightlybias:
        msg = 'Must provide --expid or --nightlybias'
        if rank == 0:
            log.critical(msg)

        sys.exit(1)

    if only_nightlybias and args.cameras is None:
        args.cameras = decode_camword('a0123456789')

    timer.stop('preflight')

    #-------------------------------------------------------------------------
    #- Create and submit a batch job if requested

    if args.batch:
        #exp_str = '{:08d}'.format(args.expid)
        if args.obstype is not None:
            jobdesc = args.obstype.lower()
        elif only_nightlybias:
            jobdesc = 'nightlybias'
        else:
            log.critical('No --obstype, but also not just nightlybias ?!?')
            sys.exit(1)

        if args.obstype == 'dark' and args.nightlybias:
            jobdesc = 'ccdcalib'

        if args.obstype == 'SCIENCE':
            # if not doing pre-stdstar fitting or stdstar fitting and if there is
            # no flag stopping flux calibration, set job to poststdstar
            if args.noprestdstarfit and args.nostdstarfit and (not args.nofluxcalib):
                jobdesc = 'poststdstar'
            # elif told not to do std or post stdstar but the flag for prestdstar isn't set,
            # then perform prestdstar
            elif (not args.noprestdstarfit) and args.nostdstarfit and args.nofluxcalib:
                jobdesc = 'prestdstar'
            #elif (not args.noprestdstarfit) and (not args.nostdstarfit) and (not args.nofluxcalib):
            #    jobdesc = 'science'
        scriptfile = create_desi_proc_batch_script(night=args.night, exp=args.expid,
                                                   cameras=args.cameras,
                                                   jobdesc=jobdesc, queue=args.queue,
                                                   runtime=args.runtime,
                                                   batch_opts=args.batch_opts,
                                                   timingfile=args.timingfile,
                                                   system_name=args.system_name)
        err = 0
        if not args.nosubmit:
            err = subprocess.call(['sbatch', scriptfile])
        sys.exit(err)

    #-------------------------------------------------------------------------
    #- Proceeding with running

    #- What are we going to do?
    if rank == 0:
        log.info('----------')
        log.info('Input {}'.format(args.input))
        log.info('Night {} expid {}'.format(args.night, args.expid))
        log.info('Obstype {}'.format(args.obstype))
        log.info('Cameras {}'.format(args.cameras))
        log.info('Output root {}'.format(desispec.io.specprod_root()))
        log.info('----------')

    #-------------------------------------------------------------------------
    #- Create nightly bias from N>>1 ZEROs, but only for B-cameras
    if args.nightlybias:
        timer.start('nightlybias')

        cmd = f"desi_compute_nightly_bias -n {args.night}"

        if rank == 0:
            log.info(f'RUNNING {cmd}')

        desispec.scripts.nightly_bias.main(cmd.split()[1:], comm=comm)
        timer.stop('nightlybias')

    #- this might be just nightly bias, with no single exposure to process
    if args.expid is None:
        if rank == 0:
            duration_seconds = time.time() - start_time
            mm = int(duration_seconds) // 60
            ss = int(duration_seconds - mm*60)

            log.info('No expid given; stopping now')
            log.info('All done at {}; duration {}m{}s'.format(
                time.asctime(), mm, ss))

        sys.exit()


    #-------------------------------------------------------------------------
    #- Create output directories if needed
    if rank == 0:
        preprocdir = os.path.dirname(findfile('preproc', args.night, args.expid, 'b0'))
        expdir = os.path.dirname(findfile('frame', args.night, args.expid, 'b0'))
        os.makedirs(preprocdir, exist_ok=True)
        os.makedirs(expdir, exist_ok=True)

    #- Wait for rank 0 to make directories before proceeding
    if comm is not None:
        comm.barrier()

    #-------------------------------------------------------------------------
    #- Preproc
    #- All obstypes get preprocessed

    timer.start('fibermap')

    #- Assemble fibermap for science exposures
    fibermap = None
    fibermap_ok = None
    if rank == 0 and args.obstype == 'SCIENCE':
        fibermap = findfile('fibermap', args.night, args.expid)
        if not os.path.exists(fibermap):
            tmp = findfile('preproc', args.night, args.expid, 'b0')
            preprocdir = os.path.dirname(tmp)
            fibermap = os.path.join(preprocdir, os.path.basename(fibermap))

            tileid = hdr['TILEID']
            # tilepix = os.path.join(preprocdir, f'tilepix-{tileid}.json')
            tilepix = findfile('tilepix', args.night, args.expid, tile=tileid)

            log.info('Creating fibermap {}'.format(fibermap))
            cmd = 'assemble_fibermap -n {} -e {} -o {} -t {}'.format(
                    args.night, args.expid, fibermap, tilepix)                  
            if args.badamps is not None:
                cmd += ' --badamps={}'.format(args.badamps)
            cmdargs = cmd.split()[1:]
            result, success = runcmd(desispec.scripts.assemble_fibermap.main,
                    args=cmdargs, inputs=[], outputs=[fibermap, tilepix])

            if not success:
                error_count += 1

        fibermap_ok = os.path.exists(fibermap)

        #- Some commissioning files didn't have coords* files that caused assemble_fibermap to fail
        #- these are well known failures with no other solution, so for those, just force creation
        #- of a fibermap with null coordinate information
        if not fibermap_ok and int(args.night) < 20200310:
            log.info("Since night is before 20200310, trying to force fibermap creation without coords file")
            cmd += ' --force'
            cmdargs = cmd.split()[1:]
            result, success = runcmd(desispec.scripts.assemble_fibermap.main,
                    args=cmdargs, inputs=[], outputs=[fibermap])

            fibermap_ok = os.path.exists(fibermap)
            if err != 0 or not fibermap_ok:
                error_count += 1

    #- If assemble_fibermap failed and obstype is SCIENCE, exit now
    if comm is not None:
        fibermap_ok = comm.bcast(fibermap_ok, root=0)

    if args.obstype == 'SCIENCE' and not fibermap_ok:
        sys.stdout.flush()
        if rank == 0:
            log.critical('assemble_fibermap failed for science exposure; exiting now')

        sys.exit(13)

    #- Wait for rank 0 to make fibermap if needed
    if comm is not None:
        fibermap = comm.bcast(fibermap, root=0)

    timer.stop('fibermap')

    if not (args.obstype in ['SCIENCE'] and args.noprestdstarfit):
        timer.start('preproc')
        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            outfile = findfile('preproc', args.night, args.expid, camera)
            outdir = os.path.dirname(outfile)
            cmd = "desi_preproc -i {} -o {} --outdir {} --cameras {}".format(
                args.input, outfile, outdir, camera)
            if args.scattered_light :
                cmd += " --scattered-light"
            if args.obstype in ['SCIENCE'] and camera[0].lower() == "b" and ( not args.no_bkgsub ) :
                cmd += " --bkgsub-for-science"
            if fibermap is not None:
                cmd += " --fibermap {}".format(fibermap)
            if not args.obstype in ['ARC'] : # never model variance for arcs
                if not args.no_model_pixel_variance and args.obstype != 'DARK' :
                    cmd += " --model-variance"
            cmdargs = cmd.split()[1:]
            result, success = runcmd(desispec.scripts.preproc.main,
                    args=cmdargs, inputs=[args.input], outputs=[outfile])
            if not success:
                error_count += 1

        timer.stop('preproc')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Get input PSFs
    timer.start('findpsf')
    input_psf = dict()
    if rank == 0 and args.obstype not in ['DARK',]:
        for camera in args.cameras :
            if args.psf is not None :
                input_psf[camera] = args.psf
            elif args.calibnight is not None :
                # look for a psfnight psf for this calib night
                psfnightfile = findfile('psfnight', args.calibnight, args.expid, camera)
                if not os.path.isfile(psfnightfile) :
                    log.error("no {}".format(psfnightfile))
                    raise IOError("no {}".format(psfnightfile))
                input_psf[camera] = psfnightfile
            else :
                # look for a psfnight psf
                psfnightfile = findfile('psfnight', args.night, args.expid, camera)
                if os.path.isfile(psfnightfile) :
                    input_psf[camera] = psfnightfile
                elif args.most_recent_calib:
                    nightfile = find_most_recent(args.night, file_type='psfnight')
                    if nightfile is None:
                        input_psf[camera] = findcalibfile([hdr, camhdr[camera]], 'PSF')
                    else:
                        input_psf[camera] = nightfile
                else :
                    input_psf[camera] = findcalibfile([hdr, camhdr[camera]], 'PSF')
            log.info("Will use input PSF : {}".format(input_psf[camera]))

    if comm is not None:
        input_psf = comm.bcast(input_psf, root=0)

    timer.stop('findpsf')


    #-------------------------------------------------------------------------
    #- Dark (to detect bad columns)

    if args.obstype == 'DARK' :

        # check exposure time and perform a dark inspection only
        # if it is a 300s exposure
        exptime = None
        if rank == 0 :
            rawfilename=findfile('raw', args.night, args.expid)
            head=fitsio.read_header(rawfilename,1)
            exptime=head["EXPTIME"]
        if comm is not None :
            exptime = comm.bcast(exptime, root=0)

        if exptime > 270 and exptime < 330 :
            timer.start('inspect_dark')
            if rank == 0 :
                log.info('Starting desi_inspect_dark at {}'.format(time.asctime()))

            for i in range(rank, len(args.cameras), size):
                camera = args.cameras[i]
                preprocfile = findfile('preproc', args.night, args.expid, camera)
                badcolumnsfile = findfile('badcolumns', night=args.night, camera=camera)
                if not os.path.isfile(badcolumnsfile) :
                    cmd = "desi_inspect_dark"
                    cmd += " -i {}".format(preprocfile)
                    cmd += " --badcol-table {}".format(badcolumnsfile)
                    cmdargs = cmd.split()[1:]
                    result, success = runcmd(desispec.scripts.inspect_dark.main,
                            args=cmdargs, inputs=[preprocfile], outputs=[badcolumnsfile])

                    if not success:
                        error_count += 1
                else:
                    log.info(f'{badcolumnsfile} already exists; skipping desi_inspect_dark')

            if comm is not None :
                comm.barrier()

            timer.stop('inspect_dark')
        elif rank == 0:
            log.warning(f'Not running desi_inspect_dark for DARK with exptime={exptime:.1f}')

    #-------------------------------------------------------------------------
    #- Traceshift

    if ( args.obstype in ['FLAT', 'TESTFLAT', 'SKY', 'TWILIGHT']     )   or \
    ( args.obstype in ['SCIENCE'] and (not args.noprestdstarfit) ):

        timer.start('traceshift')

        if rank == 0 and args.traceshift :
            log.warning('desi_proc option --traceshift is deprecated because this is now the default')

        if rank == 0 and (not args.no_traceshift) :
            log.info('Starting traceshift at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            preprocfile = findfile('preproc', args.night, args.expid, camera)
            inpsf  = input_psf[camera]
            outpsf = findfile('psf', args.night, args.expid, camera)
            if not os.path.isfile(outpsf) :
                if (not args.no_traceshift):
                    cmd = "desi_compute_trace_shifts"
                    cmd += " -i {}".format(preprocfile)
                    cmd += " --psf {}".format(inpsf)
                    cmd += " --degxx 2 --degxy 0"
                    if args.obstype in ['FLAT', 'TESTFLAT', 'TWILIGHT'] :
                        cmd += " --continuum"
                    else :
                        cmd += " --degyx 2 --degyy 0"
                    if args.obstype in ['SCIENCE', 'SKY']:
                        cmd += ' --sky'
                    cmd += " --outpsf {}".format(outpsf)
                    cmdargs = cmd.split()[1:]
                    cmd = desispec.scripts.trace_shifts.main
                    expandargs = False
                else:
                    cmdargs = (inpsf, outpsf)
                    cmd = os.symlink
                    expandargs = True

                result, success = runcmd(cmd, args=cmdargs, expandargs=expandargs,
                        inputs=[preprocfile, inpsf], outputs=[outpsf])

                if not success:
                    error_count += 1
            else :
                log.info("PSF {} exists".format(outpsf))

        timer.stop('traceshift')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- PSF
    #- MPI parallelize this step

    if args.obstype in ['ARC', 'TESTARC']:

        timer.start('arc_traceshift')

        if rank == 0:
            log.info('Starting traceshift before specex PSF fit at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            preprocfile = findfile('preproc', args.night, args.expid, camera)
            inpsf  = input_psf[camera]
            outpsf = findfile('psf', args.night, args.expid, camera)
            outpsf = replace_prefix(outpsf, "psf", "shifted-input-psf")
            if not os.path.isfile(outpsf) :
                cmd = "desi_compute_trace_shifts"
                cmd += " -i {}".format(preprocfile)
                cmd += " --psf {}".format(inpsf)
                cmd += " --degxx 0 --degxy 0 --degyx 0 --degyy 0"
                cmd += ' --arc-lamps'
                cmd += " --outpsf {}".format(outpsf)
                cmdargs = cmd.split()[1:]
                result, success = runcmd(desispec.scripts.trace_shifts.main,
                        args=cmdargs, inputs=[preprocfile, inpsf], outputs=[outpsf])
                if not success:
                    error_count += 1

            else :
                log.info("PSF {} exists".format(outpsf))

        timer.stop('arc_traceshift')
        if comm is not None:
            comm.barrier()

        timer.start('psf')

        if rank == 0:
            log.info('Starting specex PSF fitting at {}'.format(time.asctime()))

        if rank > 0:
            cmds = inputs = outputs = None
        else:
            cmds = dict()
            inputs = dict()
            outputs = dict()
            for camera in args.cameras:
                preprocfile = findfile('preproc', args.night, args.expid, camera)
                tmpname = findfile('psf', args.night, args.expid, camera)
                inpsf = replace_prefix(tmpname,"psf","shifted-input-psf")
                outpsf = replace_prefix(tmpname,"psf","fit-psf")

                log.info("now run specex psf fit")

                cmd = 'desi_compute_psf'
                cmd += ' --input-image {}'.format(preprocfile)
                cmd += ' --input-psf {}'.format(inpsf)
                cmd += ' --output-psf {}'.format(outpsf)

                # fibers to ignore for the PSF fit
                # specex uses the fiber index in a camera
                fibers_to_ignore = badfibers([hdr, camhdr[camera]],["BROKENFIBERS","BADCOLUMNFIBERS"])%500
                if fibers_to_ignore.size>0 :
                    fibers_to_ignore_str=str(fibers_to_ignore[0])
                    for fiber in fibers_to_ignore[1:] :
                        fibers_to_ignore_str+=",{}".format(fiber)
                    cmd += ' --broken-fibers {}'.format(fibers_to_ignore_str)
                    if rank == 0 :
                        log.warning('broken fibers: {}'.format(fibers_to_ignore_str))

                if not os.path.exists(outpsf):
                    cmds[camera] = cmd
                    inputs[camera] = [preprocfile, inpsf]
                    outputs[camera] = [outpsf,]

        if comm is not None:
            cmds = comm.bcast(cmds, root=0)
            if len(cmds) > 0:
                err = desispec.scripts.specex.run(comm,cmds,args.cameras)
                if err != 0:
                    error_count += 1
        else:
            log.warning('fitting PSFs without MPI parallelism; this will be SLOW')
            for camera in args.cameras:
                if camera in cmds:
                    result, success = runcmd(cmds[camera], inputs=inputs[camera], outputs=outputs[camera])
                    if not success:
                        error_count += 1

        timer.stop('psf')
        if comm is not None:
            comm.barrier()

        # loop on all cameras and interpolate bad fibers
        for camera in args.cameras[rank::size]:
            t0 = time.time()

            psfname = findfile('psf', args.night, args.expid, camera)
            inpsf = replace_prefix(psfname,"psf","fit-psf")

            #- Check if a noisy amp might have corrupted this PSF;
            #- if so, rename to *.badreadnoise
            #- Currently the data is flagged per amp (25% of pixels), but do
            #- more generic test for 12.5% of pixels (half of one amp)
            log.info(f'Rank {rank} checking for noisy input CCD amps')
            preprocfile = findfile('preproc', args.night, args.expid, camera)
            mask = fitsio.read(preprocfile, 'MASK')
            noisyfrac = np.sum((mask & ccdmask.BADREADNOISE) != 0) / mask.size
            if noisyfrac > 0.25*0.5:
                log.error(f"{100*noisyfrac:.0f}% of {camera} input pixels have bad readnoise; don't use this PSF")
                if os.path.exists(inpsf):
                    os.rename(inpsf, inpsf+'.badreadnoise')
                continue

            log.info(f'Rank {rank} interpolating {camera} PSF over bad fibers')

            # fibers to ignore for the PSF fit
            # specex uses the fiber index in a camera
            fibers_to_ignore = badfibers([hdr, camhdr[camera]],["BROKENFIBERS","BADCOLUMNFIBERS"])%500
            if fibers_to_ignore.size>0 :
                fibers_to_ignore_str=str(fibers_to_ignore[0])
                for fiber in fibers_to_ignore[1:] :
                    fibers_to_ignore_str+=",{}".format(fiber)

                outpsf = replace_prefix(psfname,"psf","fit-psf-fixed-listed")
                if os.path.isfile(inpsf) and not os.path.isfile(outpsf):
                    cmd = 'desi_interpolate_fiber_psf'
                    cmd += ' --infile {}'.format(inpsf)
                    cmd += ' --outfile {}'.format(outpsf)
                    cmd += ' --fibers {}'.format(fibers_to_ignore_str)
                    log.info('For camera {} interpolating PSF for fibers: {}'.format(camera,fibers_to_ignore_str))
                    cmdargs = cmd.split()[1:]

                    result, success = runcmd(desispec.scripts.interpolate_fiber_psf.main,
                            args=cmdargs, inputs=[inpsf], outputs=[outpsf])

                    if not success:
                        error_count += 1

                    if os.path.isfile(outpsf) :
                        os.rename(inpsf,inpsf.replace("fit-psf","fit-psf-before-listed-fix"))
                        subprocess.call('cp {} {}'.format(outpsf,inpsf),shell=True)

            dt = time.time() - t0
            log.info(f'Rank {rank} {camera} PSF interpolation took {dt:.1f} sec')    

    #-------------------------------------------------------------------------
    #- Merge PSF of night if applicable

    #if args.obstype in ['ARC']:
    if False:
        if rank == 0:
            for camera in args.cameras :
                psfnightfile = findfile('psfnight', args.night, args.expid, camera)
                if not os.path.isfile(psfnightfile) : # we still don't have a psf night, see if we can compute it ...
                    psfs = glob.glob(findfile('psf', args.night, args.expid, camera).replace("psf","fit-psf").replace(str(args.expid),"*"))
                    log.info("Number of PSF for night={} camera={} = {}".format(args.night,camera,len(psfs)))
                    if len(psfs)>4 : # lets do it!
                        log.info("Computing psfnight ...")
                        dirname=os.path.dirname(psfnightfile)
                        if not os.path.isdir(dirname) :
                            os.makedirs(dirname)
                        desispec.scripts.specex.mean_psf(psfs,psfnightfile)
                if os.path.isfile(psfnightfile) : # now use this one
                    input_psf[camera] = psfnightfile

    #-------------------------------------------------------------------------
    #- Extract
    #- This is MPI parallel so handle a bit differently

    # maybe add ARC and TESTARC too
    if ( args.obstype in ['FLAT', 'TESTFLAT', 'SKY', 'TWILIGHT']     )   or \
    ( args.obstype in ['SCIENCE'] and (not args.noprestdstarfit) ):

        timer.start('extract')
        if rank == 0:
            log.info('Starting extractions at {}'.format(time.asctime()))

        if rank > 0:
            cmds = inputs = outputs = None
        else:
            #- rank 0 collects commands to broadcast to others
            cmds = dict()
            inputs = dict()
            outputs = dict()
            for camera in args.cameras:
                cmd = 'desi_extract_spectra'

                #- Based on data from SM1-SM8, looking at central and edge fibers
                #- with in mind overlapping arc lamps lines
                if camera.startswith('b'):
                    cmd += ' -w 3600.0,5800.0,0.8'
                elif camera.startswith('r'):
                    cmd += ' -w 5760.0,7620.0,0.8'
                elif camera.startswith('z'):
                    cmd += ' -w 7520.0,9824.0,0.8'

                preprocfile = findfile('preproc', args.night, args.expid, camera)
                psffile = findfile('psf', args.night, args.expid, camera)
                finalframefile = findfile('frame', args.night, args.expid, camera)
                if os.path.exists(finalframefile):
                    log.info('{} already exists; not regenerating'.format(
                        os.path.basename(finalframefile)))
                    continue

                #- finalframefile doesn't exist; proceed with command
                framefile = finalframefile.replace(".fits","-no-badcolumn-mask.fits")
                cmd += ' -i {}'.format(preprocfile)
                cmd += ' -p {}'.format(psffile)
                cmd += ' -o {}'.format(framefile)
                cmd += ' --psferr 0.1'

                if args.gpuspecter:
                    cmd += ' --gpu-specter'
                    #- default for CPU is nsubbundles=6 but gpu_specter only allows 1, 5, or 25
                    cmd += ' --nsubbundles 5'
                    cmd += ' --mpi'

                if args.gpuextract:
                    cmd += ' --use-gpu'

                if args.obstype == 'SCIENCE' or args.obstype == 'SKY' :
                    log.info('Include barycentric correction')
                    cmd += ' --barycentric-correction'

                missing_inputs = False
                for infile in [preprocfile, psffile]:
                    if not os.path.exists(infile):
                        log.error(f'Missing {infile}')
                        missing_inputs = True

                if missing_inputs:
                    log.error(f'Camera {camera} missing inputs; skipping extractions')
                    continue

                if os.path.exists(framefile):
                    log.info(f'{framefile} already exists; skipping extraction')
                    continue

                cmds[camera] = cmd
                inputs[camera] = [preprocfile, psffile]
                outputs[camera] = [framefile,]

        #- TODO: refactor/combine this with PSF comm splitting logic
        if comm is not None:
            cmds = comm.bcast(cmds, root=0)
            inputs = comm.bcast(inputs, root=0)
            outputs = comm.bcast(outputs, root=0)

            if args.gpuextract:
                import cupy as cp
                ngpus = cp.cuda.runtime.getDeviceCount()
                if rank == 0 and len(cmds)>0:
                    log.info(f"{rank} found {ngpus} gpus")

            #- Set extraction subcomm group size
            extract_subcomm_size = args.extract_subcomm_size
            if extract_subcomm_size is None:
                if args.gpuextract:
                    #- GPU extraction with gpu_specter uses
                    #- 5 ranks per GPU plus 2 for IO.
                    extract_subcomm_size = 2 + 5 * ngpus
                elif args.gpuspecter:
                    #- CPU extraction with gpu_specter uses
                    #- 16 ranks.
                    extract_subcomm_size = 16
                else:
                    #- CPU extraction with specter uses
                    #- 20 ranks.
                    extract_subcomm_size = 20

            #- Create list of ranks that will perform extraction
            if args.gpuextract:
                #- GPU extraction uses only one extraction group
                extract_group      = 0
                num_extract_groups = 1
            else:
                #- CPU extraction uses as many extraction groups as possible
                extract_group      = rank // extract_subcomm_size
                num_extract_groups = size // extract_subcomm_size
            extract_ranks = list(range(num_extract_groups*extract_subcomm_size))

            #- Create subcomm groups
            if args.gpuextract and len(cmds)>0:
                if rank in extract_ranks:
                    #- GPU extraction
                    extract_incl = comm.group.Incl(extract_ranks)
                    comm_extract = comm.Create_group(extract_incl)
                    from gpu_specter.mpi import ParallelIOCoordinator
                    coordinator = ParallelIOCoordinator(comm_extract)
            else:
                #- CPU extraction
                comm_extract = comm.Split(color=extract_group)

            if rank in extract_ranks and len(cmds)>0:
                #- Run the extractions
                for i in range(extract_group, len(args.cameras), num_extract_groups):
                    camera = args.cameras[i]
                    if camera in cmds:
                        cmdargs = cmds[camera].split()[1:]
                        extract_args = desispec.scripts.extract.parse(cmdargs)

                        if comm_extract.rank == 0:
                            print('RUNNING: {}'.format(cmds[camera]))

                        if args.gpuextract:
                            #- GPU extraction with gpu_specter
                            desispec.scripts.extract.main_gpu_specter(extract_args, coordinator=coordinator)
                        elif args.gpuspecter:
                            #- CPU extraction with gpu_specter
                            desispec.scripts.extract.main_gpu_specter(extract_args, comm=comm_extract)
                        else:
                            #- CPU extraction with specter
                            desispec.scripts.extract.main_mpi(extract_args, comm=comm_extract)
            elif len(cmds)>0:
                #- Skip this rank
                log.warning(f'rank {rank} idle during extraction step')

            comm.barrier()

        elif len(cmds)>0:
            log.warning('running extractions without MPI parallelism; this will be SLOW')
            for camera in args.cameras:
                if camera in cmds:
                    result, success = runcmd(cmds[camera], inputs=inputs[camera], outputs=outputs[camera])
                    if not success:
                        error_count += 1

        #- check for missing output files and log
        for camera in args.cameras:
            if camera in cmds:
                for outfile in outputs[camera]:
                    if not os.path.exists(outfile):
                        if comm is not None:
                            if comm.rank > 0:
                                continue
                        log.error(f'Camera {camera} extraction missing output {outfile}')
                        error_count += 1

        timer.stop('extract')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Badcolumn specmask and fibermask
    if ( args.obstype in ['FLAT', 'TESTFLAT', 'SKY', 'TWILIGHT']     )   or \
       ( args.obstype in ['SCIENCE'] and (not args.noprestdstarfit) ):

        if rank==0 :
            log.info('Starting desi_compute_badcolumn_mask at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera     = args.cameras[i]
            outfile    = findfile('frame', args.night, args.expid, camera)
            infile     = outfile.replace(".fits","-no-badcolumn-mask.fits")
            psffile    = findfile('psf', args.night, args.expid, camera)
            badcolfile = findfile('badcolumns', night=args.night, camera=camera)
            cmd = "desi_compute_badcolumn_mask -i {} -o {} --psf {} --badcolumns {}".format(
                infile, outfile, psffile, badcolfile)

            if os.path.exists(outfile):
                log.info('{} already exists; not (re-)applying bad column mask'.format(os.path.basename(outfile)))
                continue

            if os.path.exists(badcolfile):
                cmdargs = cmd.split()[1:]

                result, success = runcmd(desispec.scripts.badcolumn_mask.main,
                        args=cmdargs, inputs=[infile,psffile,badcolfile], outputs=[outfile])

                if not success:
                    error_count += 1

                #- if successful, remove temporary frame-*-no-badcolumn-mask
                if os.path.isfile(outfile) :
                    log.info("rm "+infile)
                    os.unlink(infile)

            else:
                log.warning(f'Missing {badcolfile}; not applying badcol mask')
                log.info(f"mv {infile} {outfile}")
                os.rename(infile, outfile)

        if comm is not None :
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Fiberflat

    if args.obstype in ['FLAT', 'TESTFLAT'] :
        exptime = None
        if rank == 0 :
            rawfilename=findfile('raw', args.night, args.expid)
            head=fitsio.read_header(rawfilename,1)
            exptime=head["EXPTIME"]
        if comm is not None :
            exptime = comm.bcast(exptime, root=0)

        if exptime > 10:
            timer.start('fiberflat')
            if rank == 0:
                log.info('Flat exposure time was greater than 10 seconds')
                log.info('Starting fiberflats at {}'.format(time.asctime()))

            for i in range(rank, len(args.cameras), size):
                camera = args.cameras[i]
                framefile = findfile('frame', args.night, args.expid, camera)
                fiberflatfile = findfile('fiberflat', args.night, args.expid, camera)
                cmd = "desi_compute_fiberflat"
                cmd += " -i {}".format(framefile)
                cmd += " -o {}".format(fiberflatfile)
                cmdargs = cmd.split()[1:]

                result, success = runcmd(desispec.scripts.fiberflat.main,
                        args=cmdargs, inputs=[framefile,], outputs=[fiberflatfile,])

                if not success:
                    error_count += 1

            timer.stop('fiberflat')
            if comm is not None:
                comm.barrier()

    #-------------------------------------------------------------------------
    #- Get input fiberflat
    if args.obstype in ['SCIENCE', 'SKY'] and (not args.nofiberflat):
        timer.start('find_fiberflat')
        input_fiberflat = dict()
        if rank == 0:
            for camera in args.cameras :
                if args.fiberflat is not None :
                    input_fiberflat[camera] = args.fiberflat
                elif args.calibnight is not None :
                    # look for a fiberflatnight for this calib night
                    fiberflatnightfile = findfile('fiberflatnight',
                            args.calibnight, args.expid, camera)
                    if not os.path.isfile(fiberflatnightfile) :
                        log.error("no {}".format(fiberflatnightfile))
                        raise IOError("no {}".format(fiberflatnightfile))
                    input_fiberflat[camera] = fiberflatnightfile
                else :
                    # look for a fiberflatnight fiberflat
                    fiberflatnightfile = findfile('fiberflatnight',
                            args.night, args.expid, camera)
                    if os.path.isfile(fiberflatnightfile) :
                        input_fiberflat[camera] = fiberflatnightfile
                    elif args.most_recent_calib:
                        nightfile = find_most_recent(args.night, file_type='fiberflatnight')
                        if nightfile is None:
                            input_fiberflat[camera] = findcalibfile([hdr, camhdr[camera]], 'FIBERFLAT')
                        else:
                            input_fiberflat[camera] = nightfile
                    else :
                        input_fiberflat[camera] = findcalibfile(
                                [hdr, camhdr[camera]], 'FIBERFLAT')
                log.info("Will use input FIBERFLAT: {}".format(input_fiberflat[camera]))

        if comm is not None:
            input_fiberflat = comm.bcast(input_fiberflat, root=0)

        timer.stop('find_fiberflat')

    #-------------------------------------------------------------------------
    #- Fiber flat corrected for humidity
    if args.obstype in ['SCIENCE', 'SKY'] and (not args.noprestdstarfit):

        timer.start('fiberflat_humidity_correction')

        if rank == 0:
            log.info('Flatfield correction for humidity {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            framefile = findfile('frame', args.night, args.expid, camera)
            hdr = fitsio.read_header(framefile, 'FLUX')
            input_fiberflatfile=input_fiberflat[camera]
            if input_fiberflatfile is None :
                log.error("No input fiberflat for {}".format(camera))
                continue

            # First need a flatfield per exposure
            fiberflatfile = findfile('fiberflatexp', args.night, args.expid, camera)

            cmd = "desi_compute_humidity_corrected_fiberflat"
            cmd += " --use-sky-fibers"
            cmd += " -i {}".format(framefile)
            cmd += " --fiberflat {}".format(input_fiberflatfile)
            cmd += " -o {}".format(fiberflatfile)
            cmdargs = cmd.split()[1:]

            result, success = runcmd(desispec.scripts.humidity_corrected_fiberflat.main,
                    args=cmdargs, inputs=[framefile, input_fiberflatfile], outputs=[fiberflatfile,])

            if not success:
                error_count += 1

        timer.stop('fiberflat_humidity_correction')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Apply fiberflat and write fframe file

    if args.obstype in ['SCIENCE', 'SKY'] and args.fframe and \
    ( not args.nofiberflat ) and (not args.noprestdstarfit):
        timer.start('apply_fiberflat')
        if rank == 0:
            log.info('Applying fiberflat at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            fframefile = findfile('fframe', args.night, args.expid, camera)
            if not os.path.exists(fframefile):
                framefile = findfile('frame', args.night, args.expid, camera)
                fr = desispec.io.read_frame(framefile)
                flatfilename = findfile('fiberflatexp', args.night, args.expid, camera)
                ff = desispec.io.read_fiberflat(flatfilename)
                fr.meta['FIBERFLT'] = desispec.io.shorten_filename(flatfilename)
                apply_fiberflat(fr, ff)
                fframefile = findfile('fframe', args.night, args.expid, camera)
                desispec.io.write_frame(fframefile, fr)

        timer.stop('apply_fiberflat')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Select random sky fibers (inplace update of frame file)
    #- TODO: move this to a function somewhere
    #- TODO: this assigns different sky fibers to each frame of same spectrograph

    if (args.obstype in ['SKY', 'SCIENCE']) and (not args.noskysub) and (not args.noprestdstarfit):
        timer.start('picksky')
        if rank == 0:
            log.info('Picking sky fibers at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            framefile = findfile('frame', args.night, args.expid, camera)
            orig_frame = desispec.io.read_frame(framefile)

            #- Make a copy so that we can apply fiberflat
            fr = deepcopy(orig_frame)

            if np.any(fr.fibermap['OBJTYPE'] == 'SKY'):
                log.info('{} sky fibers already set; skipping'.format(
                    os.path.basename(framefile)))
                continue

            #- Apply fiberflat then select random fibers below a flux cut
            flatfilename = findfile('fiberflatexp', args.night, args.expid, camera)
            ff = desispec.io.read_fiberflat(flatfilename)
            apply_fiberflat(fr, ff)
            sumflux = np.sum(fr.flux, axis=1)
            fluxcut = np.percentile(sumflux, 30)
            iisky = np.where(sumflux < fluxcut)[0]
            iisky = np.random.choice(iisky, size=100, replace=False)

            #- Update fibermap or original frame and write out
            orig_frame.fibermap['OBJTYPE'][iisky] = 'SKY'
            orig_frame.fibermap['DESI_TARGET'][iisky] |= desi_mask.SKY

            desispec.io.write_frame(framefile, orig_frame)

        timer.stop('picksky')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Sky subtraction
    if args.obstype in ['SCIENCE', 'SKY'] and (not args.noskysub ) and (not args.noprestdstarfit):

        timer.start('skysub')
        if rank == 0:
            log.info('Starting sky subtraction at {}'.format(time.asctime()))

        for i in range(rank, len(args.cameras), size):
            camera = args.cameras[i]
            framefile = findfile('frame', args.night, args.expid, camera)
            hdr = fitsio.read_header(framefile, 'FLUX')
            fiberflatfile = findfile('fiberflatexp', args.night, args.expid, camera)
            skyfile = findfile('sky', args.night, args.expid, camera)

            cmd = "desi_compute_sky"
            cmd += " -i {}".format(framefile)
            cmd += " --fiberflat {}".format(fiberflatfile)
            cmd += " -o {}".format(skyfile)
            if args.no_extra_variance :
                cmd += " --no-extra-variance"
            if not args.no_sky_wavelength_adjustment : cmd += " --adjust-wavelength"
            if not args.no_sky_lsf_adjustment : cmd += " --adjust-lsf"
            if (not args.no_sky_wavelength_adjustment) and (not args.no_sky_lsf_adjustment) and args.save_sky_adjustments :
                cmd += " --save-adjustments {}".format(skyfile.replace("sky-","skycorr-"))
            if args.adjust_sky_with_more_fibers :
                cmd += " --adjust-with-more-fibers"
            if (not args.no_sky_wavelength_adjustment) or (not args.no_sky_lsf_adjustment) :
                pca_corr_filename = findcalibfile([hdr, camhdr[camera]], 'SKYCORR')
                if pca_corr_filename is not None :
                    cmd += " --pca-corr {}".format(pca_corr_filename)
                else :
                    log.warning("No SKYCORR file, do you need to update DESI_SPECTRO_CALIB?")
            cmd += " --fit-offsets"
            if args.skygradpca:
                skygradpca_filename = findcalibfile([hdr, camhdr[camera]], 'SKYGRADPCA')
                if skygradpca_filename is not None :
                    cmd += " --skygradpca {}".format(skygradpca_filename)
                else :
                    log.warning("No SKYGRADPCA file, do you need to update DESI_SPECTRO_CALIB?")

            cmdargs = cmd.split()[1:]

            result, success = runcmd(desispec.scripts.sky.main,
                    args=cmdargs, inputs=[framefile, fiberflatfile], outputs=[skyfile,])

            if not success:
                error_count += 1

            #- sframe = flatfielded sky-subtracted but not flux calibrated frame
            #- Note: this re-reads and re-does steps previously done for picking
            #- sky fibers; desi_proc is about human efficiency,
            #- not I/O or CPU efficiency...
            sframefile = desispec.io.findfile('sframe', args.night, args.expid, camera)
            if not os.path.exists(sframefile):
                missing_inputs = False
                for filename in [framefile, fiberflatfile, skyfile]:
                    if not os.path.exists(filename):
                        log.error(f'Camera {camera} missing sframe input {filename}')
                        missing_inputs = True

                if missing_inputs:
                    log.error(f'Camera {camera} missing sframe inputs; skipping')
                    error_count += 1
                else:
                    try:
                        frame = desispec.io.read_frame(framefile)
                        fiberflat = desispec.io.read_fiberflat(fiberflatfile)
                        sky = desispec.io.read_sky(skyfile)
                        apply_fiberflat(frame, fiberflat)
                        subtract_sky(frame, sky, apply_throughput_correction=True)
                        frame.meta['IN_SKY'] = shorten_filename(skyfile)
                        frame.meta['FIBERFLT'] = shorten_filename(fiberflatfile)
                        desispec.io.write_frame(sframefile, frame)
                    except Exception as err:
                        import traceback
                        lines = traceback.format_exception(*sys.exc_info())
                        log.error(f"Camera {camera} sframe raised an exception:")
                        print("".join(lines))
                        log.warning(f'Continuing without {sframefile}')
                        error_count += 1

        timer.stop('skysub')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Standard Star Fitting

    if args.obstype in ['SCIENCE',] and \
            (not args.noskysub ) and \
            (not args.nostdstarfit) :

        timer.start('stdstarfit')
        if rank == 0:
            log.info('Starting flux calibration at {}'.format(time.asctime()))

        #- Group inputs by spectrograph
        framefiles = dict()
        skyfiles = dict()
        fiberflatfiles = dict()
        night, expid = args.night, args.expid #- shorter
        for camera in args.cameras:
            sp = int(camera[1])
            if sp not in framefiles:
                framefiles[sp] = list()
                skyfiles[sp] = list()
                fiberflatfiles[sp] = list()

            framefiles[sp].append(findfile('frame', night, expid, camera))
            skyfiles[sp].append(findfile('sky', night, expid, camera))
            fiberflatfiles[sp].append(findfile('fiberflatexp', night, expid, camera))

        #- Hardcoded stdstar model version
        starmodels = os.path.join(
            os.getenv('DESI_BASIS_TEMPLATES'), 'stdstar_templates_v2.2.fits')

        #- Fit stdstars per spectrograph (not per-camera)
        spectro_nums = sorted(framefiles.keys())

        if args.mpistdstars and comm is not None:
            #- If using MPI parallelism in stdstar fit, divide comm into subcommunicators.
            #- (spectro_start, spectro_step) determine stride pattern over spectro_nums.
            #- Split comm by at most len(spectro_nums)
            num_subcomms = min(size, len(spectro_nums))
            subcomm_index = rank % num_subcomms
            if rank == 0:
                log.info(f"Splitting comm of {size=} into {num_subcomms=} for stdstar fitting")
            subcomm = comm.Split(color=subcomm_index)
            spectro_start, spectro_step = subcomm_index, num_subcomms
        else:
            #- Otherwise, use multiprocessing assuming 1 MPI rank per spectrograph
            spectro_start, spectro_step = rank, size
            subcomm = None

        for i in range(spectro_start, len(spectro_nums), spectro_step):
            sp = spectro_nums[i]

            stdfile = findfile('stdstars', night, expid, spectrograph=sp)
            cmd = "desi_fit_stdstars"
            cmd += " --frames {}".format(' '.join(framefiles[sp]))
            cmd += " --skymodels {}".format(' '.join(skyfiles[sp]))
            cmd += " --fiberflats {}".format(' '.join(fiberflatfiles[sp]))
            cmd += " --starmodels {}".format(starmodels)
            cmd += " --outfile {}".format(stdfile)
            cmd += " --delta-color 0.1"
            if args.maxstdstars is not None:
                cmd += " --maxstdstars {}".format(args.maxstdstars)

            inputs = framefiles[sp] + skyfiles[sp] + fiberflatfiles[sp]
            err = 0
            cmdargs = cmd.split()[1:]

            if subcomm is None:
                #- Using multiprocessing
                log.info(f'Rank {rank=} fitting sp{sp=} stdstars with multiprocessing')
                result, success = runcmd(desispec.scripts.stdstars.main,
                    args=[cmdargs], inputs=inputs, outputs=[stdfile])
            else:
                #- Using MPI
                log.info(f'Rank {rank=} fitting sp{sp=} stdstars with mpi')
                result, success = runcmd(desispec.scripts.stdstars.main,
                    args=cmdargs, inputs=inputs, outputs=[stdfile], comm=subcomm)

            if not success:
                log.info(f'Rank {rank=} stdstar failure {err=}')
                error_count += 1

        timer.stop('stdstarfit')
        if comm is not None:
            comm.barrier()

    # -------------------------------------------------------------------------
    # - Flux calibration

    def list2str(xx) :
        """converts list xx to string even if elements aren't strings"""
        return " ".join([str(x) for x in xx])

    if args.obstype in ['SCIENCE'] and \
                (not args.noskysub) and \
                (not args.nofluxcalib):
        timer.start('fluxcalib')

        night, expid = args.night, args.expid #- shorter

        if rank == 0 :
            r_cameras = []
            for camera in args.cameras :
                if camera[0] == 'r' :
                    r_cameras.append(camera)
            if len(r_cameras)>0 :
                outfile    = findfile('calibstars',night, expid)
                frames     = [findfile('frame', night, expid, camera) for camera in r_cameras]
                fiberflats = [findfile('fiberflatexp', night, expid, camera) for camera in r_cameras]
                skys       = [findfile('sky', night, expid, camera) for camera in r_cameras]
                models     = [findfile('stdstars', night, expid,spectrograph=int(camera[1])) for camera in r_cameras]

                inputs = frames + fiberflats + skys + models
                cmd = "desi_select_calib_stars --delta-color-cut 0.1 "
                cmd += " --frames {}".format(list2str(frames))
                cmd += " --fiberflats {}".format(list2str(fiberflats))
                cmd += " --skys {}".format(list2str(skys))
                cmd += " --models {}".format(list2str(models))
                cmd += f" -o {outfile}"
                cmdargs = cmd.split()[1:]
                result, success = runcmd(desispec.scripts.select_calib_stars.main,
                        args=cmdargs, inputs=inputs, outputs=[outfile,])

                if not success:
                    error_count += 1

        if comm is not None:
            comm.barrier()

        #- Compute flux calibration vectors per camera
        for camera in args.cameras[rank::size]:
            framefile = findfile('frame', night, expid, camera)
            skyfile = findfile('sky', night, expid, camera)
            spectrograph = int(camera[1])
            stdfile = findfile('stdstars', night, expid,spectrograph=spectrograph)
            calibfile = findfile('fluxcalib', night, expid, camera)
            calibstars = findfile('calibstars',night, expid)
            fiberflatfile = findfile('fiberflatexp', night, expid, camera)

            cmd = "desi_compute_fluxcalibration"
            cmd += " --infile {}".format(framefile)
            cmd += " --sky {}".format(skyfile)
            cmd += " --fiberflat {}".format(fiberflatfile)
            cmd += " --models {}".format(stdfile)
            cmd += " --outfile {}".format(calibfile)
            cmd += " --selected-calibration-stars {}".format(calibstars)

            inputs = [framefile, skyfile, fiberflatfile, stdfile, calibstars]
            cmdargs = cmd.split()[1:]

            result, success = runcmd(desispec.scripts.fluxcalibration.main,
                    args=cmdargs, inputs=inputs, outputs=[calibfile,])

            if not success:
                error_count += 1

        timer.stop('fluxcalib')
        if comm is not None:
            comm.barrier()

    #-------------------------------------------------------------------------
    #- Applying flux calibration

    if args.obstype in ['SCIENCE',] and (not args.noskysub ) and (not args.nofluxcalib) :

        night, expid = args.night, args.expid #- shorter

        timer.start('applycalib')
        if rank == 0:
            log.info('Starting cframe file creation at {}'.format(time.asctime()))

        for camera in args.cameras[rank::size]:
            framefile = findfile('frame', night, expid, camera)
            skyfile = findfile('sky', night, expid, camera)
            spectrograph = int(camera[1])
            stdfile = findfile('stdstars', night, expid, spectrograph=spectrograph)
            calibfile = findfile('fluxcalib', night, expid, camera)
            cframefile = findfile('cframe', night, expid, camera)
            fiberflatfile = findfile('fiberflatexp', night, expid, camera)

            cmd = "desi_process_exposure"
            cmd += " --infile {}".format(framefile)
            cmd += " --fiberflat {}".format(fiberflatfile)
            cmd += " --sky {}".format(skyfile)
            cmd += " --calib {}".format(calibfile)
            cmd += " --outfile {}".format(cframefile)
            cmd += " --cosmics-nsig 6"
            if args.no_xtalk :
                cmd += " --no-xtalk"

            inputs = [framefile, fiberflatfile, skyfile, calibfile]
            cmdargs = cmd.split()[1:]

            result, success = runcmd(desispec.scripts.procexp.main, args=cmdargs, inputs=inputs, outputs=[cframefile,])

            if not success:
                error_count += 1

        if comm is not None:
            comm.barrier()

        timer.stop('applycalib')

    #-------------------------------------------------------------------------
    #- Exposure QA, using same criterion as fluxcalib for when to run

    if args.obstype in ['SCIENCE',] and (not args.noskysub ) and (not args.nofluxcalib) :
        from desispec.scripts import exposure_qa

        night, expid = args.night, args.expid #- shorter

        timer.start('exposure_qa')
        if rank == 0:
            log.info('Starting exposure_qa at {}'.format(time.asctime()))

        #- exposure QA not yet parallelized for a single exposure
        if rank == 0:
            qa_args = ['-n', str(night), '-e', str(expid), '--nproc', str(1)]
            try:
                exposure_qa.main(exposure_qa.parse(qa_args))
            except Exception as err:
                #- log exceptions, but don't treat QA problems as fatal
                import traceback
                lines = traceback.format_exception(*sys.exc_info())
                log.error(f"exposure_qa raised an exception:")
                print("".join(lines))
                log.warning(f"QA exception not treated as blocking failure")

        #- Make other ranks wait anyway
        if comm is not None:
            comm.barrier()

        timer.stop('exposure_qa')

    #-------------------------------------------------------------------------
    #- Collect error count
    if comm is not None:
        all_error_counts = comm.gather(error_count, root=0)
        error_count = int(comm.bcast(np.sum(all_error_counts), root=0))

    if rank == 0 and error_count > 0:
        log.error(f'{error_count} processing errors; see logs above')

    #-------------------------------------------------------------------------
    #- Wrap up

    _log_timer(timer, args.timingfile, comm=comm)
    if rank == 0:
        duration_seconds = time.time() - start_time
        mm = int(duration_seconds) // 60
        ss = int(duration_seconds - mm*60)

        log.info('All done at {}; duration {}m{}s'.format(
            time.asctime(), mm, ss))

    if error_count > 0:
        sys.exit(int(error_count))
    else:
        return 0
