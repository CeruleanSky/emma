#!/usr/bin/python3
# ----------------------------------------------------
# Electromagnetic Mining Array (EMMA)
# Copyright 2017, Pieter Robyns
# ----------------------------------------------------

from ops import *
from debug import DEBUG
from time import sleep
from emma_worker import app, backend
from celery import group, chord, chain
import numpy as np
import matplotlib.pyplot as plt
import sys
import argparse
import configparser
import emutils
import emio
import subprocess
import time

def partition_work(trace_set_paths, conf):
    num_partitions = min(conf.max_subtasks, len(trace_set_paths))
    result = []
    for part in emutils.partition(trace_set_paths, num_partitions):
        result.append(work.si(part, conf))
    return chord(result, body=merge.s(conf))

def args_epilog():
    result = "Actions can take the following parameters between square brackets ('[]'):\n"
    for op in ops.keys():
        result += "{:>20s} ".format(op)
        if op in ops_optargs:
            result += "["
            for optarg in ops_optargs[op]:
                result += "{:s}, ".format(optarg)
            result = result.strip().rstrip(',')
            result += "]"
        result += "\n"
    return result

def clear_redis():
    '''
    Clear any previous results from Redis. Sadly, there is no cleaner way atm.
    '''
    try:
        subprocess.check_output(["redis-cli", "flushall"])
        logger.info("Redis cleared")
    except FileNotFoundError:
        logger.warning("Could not clear local Redis database")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Electromagnetic Mining Array (EMMA)', epilog=args_epilog(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('actions', type=str, help='Action to perform. Choose from %s' % str(ops.keys()), nargs='+')
    parser.add_argument('inpath', type=str, help='Input path where the trace sets are located')
    parser.add_argument('--inform', dest='inform', type=str, choices=['cw','sigmf','gnuradio'], default='cw', help='Input format to use when loading')
    parser.add_argument('--outform', dest='outform', type=str, choices=['cw','sigmf','gnuradio'], default='sigmf', help='Output format to use when saving')
    parser.add_argument('--outpath', '-O', dest='outpath', type=str, default='./export/', help='Output path to use when saving')
    parser.add_argument('--max-subtasks', type=int, default=2, help='Maximum number of subtasks')
    parser.add_argument('--num-subkeys', type=int, default=16, help='Number of subkeys to break')
    parser.add_argument('--kill-workers', default=False, action='store_true', help='Kill workers after finishing the tasks.')
    parser.add_argument('--butter-order', type=int, default=1, help='Order of Butterworth filter')
    parser.add_argument('--butter-cutoff', type=float, default=0.01, help='Cutoff of Butterworth filter')
    parser.add_argument('--reference-index', type=int, default=0, help='Index of reference signal')
    parser.add_argument('--windowing-method', type=str, default='rectangular', help='Windowing method')
    args, unknown = parser.parse_known_args()
    print(emutils.BANNER)

    try:
        clear_redis()

        # Get a list of filenames depending on the format
        trace_set_paths = emio.get_trace_paths(args.inpath, args.inform)

        # Worker-specific configuration
        conf = argparse.Namespace(
            reference_signal=emio.get_trace_set(trace_set_paths[0], args.inform, ignore_malformed=False).traces[args.reference_index].signal,
            **args.__dict__
        )

        # Don't allow multiple instances to train AI because Tensorflow is not thread safe when
        # using the same session
        if 'memtrain' in conf.actions:
            conf.max_subtasks = 1

        task = partition_work(trace_set_paths, conf)
        async_result = task()
        count = 0
        while not async_result.ready():
            print("\rElapsed: %d" % count, end='')
            count += 1
            time.sleep(1)
        print("")

        if not async_result.result is None:
            if not async_result.result.correlations is None:
                result = async_result.result.correlations
                print("Num entries: %d" % result._n[0][0][0])

                # Print results
                max_correlations = np.zeros([16, 256])
                for subkey_idx in range(0, conf.num_subkeys):
                    for subkey_guess in range(0, 256):
                        max_correlations[subkey_idx, subkey_guess] = np.max(np.abs(result[subkey_idx,subkey_guess,:]))
                emutils.pretty_print_correlations(max_correlations, limit_rows=20)

                # Print key
                most_likely_bytes = np.argmax(max_correlations, axis=1)
                print(emutils.numpy_to_hex(most_likely_bytes))
    except KeyboardInterrupt:
        pass

    # Clean up
    print("Cleaning up")
    app.control.purge()
    app.backend.cleanup()
    if args.kill_workers:
        subprocess.check_output(['pkill', '-9', '-f', 'celery'])
