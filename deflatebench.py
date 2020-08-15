#!/usr/bin/python3 -OOB
""" deflatebench.py -- A util that benchmarks minigzip/minideflate.

    Copyright (C) 2016-2020 Hans Kristian Rosbach

    This software is provided 'as-is', without any express or implied
    warranty. In no event will the authors be held liable for any damages
    arising from the use of this software.

    Permission is granted to anyone to use this software for any purpose,
    including commercial applications, and to alter it and redistribute it
    freely, subject to the following restrictions:

    1. The origin of this software must not be misrepresented; you must not
       claim that you wrote the original software. If you use this software
       in a product, an acknowledgment in the product documentation would be
       appreciated but is not required.
    2. Altered source versions must be plainly marked as such, and must not be
       misrepresented as being the original software.
    3. This notice may not be removed or altered from any source distribution.
"""

import os, os.path
import sys
import re
import math
import tempfile
import time
import toml
import shlex
import shutil
import hashlib
import argparse
import subprocess
from subprocess import PIPE
from datetime import datetime, date, timedelta

BUF_SIZE = 1024*1024  # lets read stuff in 1MB chunks when hashing or copying

# ANSI color codes
BLUE = '\033[34m'
GREEN = '\033[32m'
RED = '\033[31m'
BRIGHT = '\033[1m'
DIM = '\033[2m'
RESET = '\033[0m'

strip_ANSI_regex = re.compile(r"""
    \x1b     # literal ESC
    \[       # literal [
    [;\d]*   # zero or more digits or semicolons
    [A-Za-z] # a letter
    """, re.VERBOSE).sub

def get_len(s):
    ''' Return string length excluding ANSI escape strings '''
    return len(strip_ANSI_regex("", s))

def resultstr(min, avg, max):
    ''' Build result string '''
    return f"{BLUE}{min:.3f}{RESET}/{GREEN}{avg:.3f}{RESET}/{RED}{max:.3f}{RESET}"

def printnn(text):
    ''' Print without causing a newline '''
    sys.stdout.write(text)
    sys.stdout.flush()

def defconfig():
    ''' Define default config '''
    config = dict()
    config['Testruns'] = {  'runs': 15,
                            'trimworst': 5,
                            'minlevel': 0,
                            'maxlevel': 9,
                            'testmode': 'single',  # generate / multi / single
                            'testtool': 'minigzip' } # minigzip / minideflate

    config['Config'] = {'temp_path': tempfile.gettempdir(),
                        'use_perf': True,
                        'start_delay': 0,   # Milliseconds of startup to skip measuring, requires usleep(X*1000) in minigzip/minideflate main()
                        'skipverify': False,
                        'skipdecomp': False}

    ## CPU related settings
    config['Tuning'] = {'use_chrt': False,
                        'use_nosync': False,
                        'use_turboctl': False,
                        'use_cpupower': False,
                        'cpu_std_minspeed': 1000,
                        'cpu_std_maxspeed': 2200,
                        'cpu_bench_speed': 2000 }

    # Single testfile
    config['Testdata_Single'] = { 'testfile': 'silesia.tar' }

    # Multiple testfiles
    config['Testdata_Multi'] = {'0': 'testfile-500M',
                                '1': 'testfile-300M',
                                '2': 'testfile-150M',
                                '3': 'testfile-125M',
                                '4': 'testfile-100M',
                                '5': 'testfile-85M',
                                '6': 'testfile-75M',
                                '7': 'testfile-40M',
                                '8': 'testfile-20M',
                                '9': 'testfile-20M' }

    # Generated testfiles
    config['Testdata_Gen'] =  { 'srcFile': 'silesia-small.tar',
                                '0': 500,
                                '1': 270,
                                '2': 135,
                                '3': 105,
                                '4': 90,
                                '5': 90,
                                '6': 75,
                                '7': 60,
                                '8': 45,
                                '9': 45 }
    return config

def parseconfig(file):
    ''' Parse config file '''
    config = toml.load(file)
    return config

def writeconfig(file):
    ''' Write default config to file '''
    config = defconfig()
    with open(file, 'w') as f:
        toml.dump(config,f)

def mergeconfig(src, chg):
    ''' Merge config settings from chg into src '''
    if 'Testruns' in chg:
        src['Testruns'].update(chg['Testruns'])
    if 'Config' in chg:
        src['Config'].update(chg['Config'])
    if 'Tuning' in chg:
        src['Tuning'].update(chg['Tuning'])
    if 'Testdata_Gen' in chg:
        src['Testdata_Gen'].update(chg['Testdata_Gen'])
    if 'Testdata_Single' in chg:
        src['Testdata_Single'].update(chg['Testdata_Single'])
    if 'Testdata_Multi' in chg:
        src['Testdata_Multi'].update(chg['Testdata_Multi'])
    return src

def cputweak(enable):
    ''' Disable turbo, disable idlestates, and set fixed cpu mhz. Requires sudo rights. '''
    # Turn off cpu turbo and power savings
    if enable:
        if cfgTuning['use_turboctl']:
            runcommand('sudo /usr/bin/turboctl off', silent=1)
        if cfgTuning['use_cpupower']:
            runcommand(f"sudo /usr/bin/cpupower frequency-set -g performance --min {cfgTuning['cpu_bench_speed']*1000} --max {cfgTuning['cpu_bench_speed']*1000}", silent=1)
            runcommand('sudo /usr/bin/cpupower idle-set -D 2', silent=1)

    # Turn cpu turbo and power savings back on
    if not enable:
        if cfgTuning['use_turboctl']:
            runcommand('sudo /usr/bin/turboctl on')
        if cfgTuning['use_cpupower']:
            runcommand(f"sudo /usr/bin/cpupower frequency-set --min {cfgTuning['cpu_std_minspeed']*1000} --max {cfgTuning['cpu_std_maxspeed']*1000}")
            runcommand('sudo /usr/bin/cpupower idle-set -E')

def findfile(filename,fatal=True):
    ''' Search for filename in CWD, homedir and deflatebench.py-dir '''
    filepath = os.path.dirname(os.path.realpath(__file__))
    tmpCwd = os.path.join( os.getcwd(), filename)
    tmpHome = os.path.join( os.path.expanduser("~"), filename)
    tmpScript = os.path.join(filepath, filename)
    if os.path.isfile(tmpCwd):
        return os.path.realpath(tmpCwd)
    elif os.path.isfile(tmpHome):
        return os.path.realpath(tmpHome)
    elif os.path.isfile(tmpScript):
        return os.path.realpath(tmpScript)

    if fatal:
        print(f"Unable to find file: '{filename}'")
        sys.exit(1)
    return None

def hashfile(file):
    ''' Calculate hash of file '''
    sha1 = hashlib.sha1()

    with open(file, 'rb') as f:
        while True:
            data = f.read(BUF_SIZE)
            if not data:
                break
            sha1.update(data)
    return sha1.hexdigest()

def generate_testfile(sourcefile,destfile,minsize):
    ''' Make tempfiles that are concatenated repeatedly until the file is big enough '''
    srcsize = os.path.getsize(sourcefile)
    count = math.ceil((minsize*1024*1024)/srcsize)
    dstsize = srcsize*count

    dst = open(destfile, "wb")
    with open(sourcefile, 'rb') as src:
        while os.path.getsize(destfile) < dstsize:
            data = src.read(BUF_SIZE)
            if not data:
                src.seek(0)
                continue
            dst.write(data)
    dst.close()

def runcommand(command, env=None, stoponfail=1, silent=1, output=os.devnull):
    ''' Run command, and handle special cases '''
    args = shlex.split(command, posix=sys.platform != 'win32')
    sp_args = {}
    if (sys.platform == 'win32'):
        sp_args['creationflags'] = subprocess.HIGH_PRIORITY_CLASS
    if (silent == 1):
        devnull = open(output, 'w')
        retval = subprocess.call(args,env=env,stdout=devnull,**sp_args)
        devnull.close()
    else:
        retval = subprocess.call(args,env=env,**sp_args)
    if ((retval != 0) and (stoponfail != 0)):
        sys.exit(f"Failed, retval({retval}): {command}")
    return retval

def get_env(bench=False):
    env = dict()
    if bench and cfgTuning['use_nosync']:
        env['LD_PRELOAD'] = '/usr/lib64/nosync/nosync.so'
    return env

def command_prefix(timefile):
    ''' Build the benchmarking command prefix '''
    if cfgTuning['use_chrt']:
        command = "/usr/bin/chrt -f 99"
    else:
        command = "/usr/bin/nice -n -20"

    if cfgConfig['use_perf']:
        command += f" /usr/bin/perf stat -D {cfgConfig['start_delay']} -e cpu-clock:u -o '{timefile}' -- "
    else:
        timeformat="%U"
        command += f" -20 /usr/bin/time -o {timefile} -f '{timeformat}' -- "

    return command

def parse_timefile(filen):
    ''' Parse output from perf or time '''
    if cfgConfig['use_perf']:
        with open(filen) as f:
            content = f.readlines()
        for line in content:
            if line[-13:-1] == 'seconds user':
                return float(line[:-13])
        return 0.0
    else:
        with open(filen) as f:
            content = f.readlines()
        return float(content[0])

def runtest(tempfiles,level):
    ''' Run benchmark and tests for current compression level'''
    hashfail, decomptime = 0,0
    testfile = tempfiles[level]['filename']
    orighash = tempfiles[level]['hash']
    cmdprefix = ''

    env = get_env(True)

    sys.stdout.write(f"Testing level {level}: ")
    if sys.platform != 'win32':
        cmdprefix = command_prefix(timefile)
        runcommand('sync')

    # Compress
    printnn('c')
    starttime = time.perf_counter()
    testtool = os.path.realpath(cfgRuns['testtool'])

    runcommand(f"{cmdprefix} {testtool} -{level} -c {testfile}", env=env, output=compfile)
    if sys.platform != 'win32':
        comptime = parse_timefile(timefile)
    else:
        comptime = time.perf_counter() - starttime
    compsize = os.path.getsize(compfile)

    # Decompress
    if not cfgConfig['skipdecomp'] or not cfgConfig['skipverify']:
        printnn('d')
        starttime = time.perf_counter()
        runcommand(f"{cmdprefix} {testtool} -d -c {compfile}", env=env, output=decompfile)

        if sys.platform != 'win32':
            decomptime = parse_timefile(timefile)
        else:
            decomptime = time.perf_counter() - starttime

        if not cfgConfig['skipverify']:
            ourhash = hashfile(decompfile)
            if ourhash != orighash:
                print(f"{orighash} != {ourhash}")
                hashfail = 1

        os.unlink(decompfile)

    # Validate using gunzip
    if not cfgConfig['skipverify']:
        printnn('v')
        runcommand(f"gunzip -c {compfile}", output=decompfile)

        gziphash = hashfile(decompfile)
        if gziphash != orighash:
            print(f"{orighash} != {gziphash}")
            hashfail = 1

        os.unlink(decompfile)

    if os.path.exists(timefile):
        os.unlink(timefile)
    os.unlink(compfile)

    printnn(f" {comptime:7.3f} {decomptime:7.3f} {compsize}")
    printnn('\n')

    return compsize,comptime,decomptime,hashfail

def trimworst(results):
    ''' Trim X worst results '''
    results.sort()
    if cfgRuns['trimworst'] == 0:
        return results
    return results[:-cfgRuns['trimworst']]

def printreport(results, tempfiles):
    ''' Print results table '''
    totsize, totsize2 = [0]*2
    totorigsize, totorigsize2 = [0]*2
    totcomppct, totcomppct2 = [0]*2
    totcomptime, totcomptime2 = [0]*2
    totdecomptime, totdecomptime2 = [0]*2
    runs = cfgRuns['runs']
    numresults = runs - cfgRuns['trimworst']

    numlevels = len(range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1))

    # Print config info
    print(f"\n")
    print(f" Tool: {cfgRuns['testtool']}")
    print(f" Runs: {runs}")
    print(f" Levels: {cfgRuns['minlevel']}-{cfgRuns['maxlevel']}")
    print(f" Trimworst: {cfgRuns['trimworst']}")

    # Print header
    if cfgConfig['skipdecomp']:
        print("\n Level   Comp   Comptime min/avg/max                          Compressed size")
    else:
        print("\n Level   Comp   Comptime min/avg/max  Decomptime min/avg/max  Compressed size")

    # Calculate and print stats per level
    for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
        ltotcomptime, ltotdecomptime = [0,0]
        origsize = tempfiles[level]['origsize']

        # Find best/worst times for this level
        compsize = None
        rawcomptimes = []
        rawdecomptimes = []
        for run in results[level]:
            rsize,rcompt,rdecompt = run
            rawcomptimes.append(rcompt)
            rawdecomptimes.append(rdecompt)
            if not compsize is None and compsize != rsize:
                print(f"Warning: size changed between runs. Expected: {compsize} Got: {rsize}")
            else:
                compsize = rsize

        # Trim the worst results
        comptimes = trimworst(rawcomptimes)
        decomptimes = trimworst(rawdecomptimes)

        # Calculate min/max and sum for this level
        mincomptime = min(comptimes)
        mindecomptime = min(decomptimes)

        maxcomptime = max(comptimes)
        maxdecomptime = max(decomptimes)

        ltotcomptime += sum(comptimes)
        ltotdecomptime += sum(decomptimes)

        # Compute and print values for this level
        comppct = float(rsize*100)/origsize
        avgcomptime = ltotcomptime/numresults
        avgdecomptime = ltotdecomptime/numresults

        # Store values for grand total
        totsize += rsize
        totorigsize += origsize
        totcomppct += comppct
        totcomptime += ltotcomptime
        totdecomptime += ltotdecomptime
        if level != 0:
            totsize2 += rsize
            totorigsize2 += origsize
            totcomppct2 += comppct
            totcomptime2 += ltotcomptime
            totdecomptime2 += ltotdecomptime

        # Print level results
        compstr = resultstr(mincomptime, avgcomptime, maxcomptime)
        if cfgConfig['skipdecomp']:
            decompstr = ""
        else:
            decompstr = resultstr(mindecomptime, avgdecomptime, maxdecomptime)
        compstrpad = ' ' * (20 - get_len(compstr))
        decompstrpad = ' ' * (23 - get_len(decompstr))

        print(f" {level:5} {comppct:7.3f}% {compstrpad}{compstr} {decompstrpad}{decompstr}  {compsize} ")

    ### Totals
    # Compression
    avgcomppct = totcomppct/numlevels
    avgcomptime = totcomptime/(numlevels*numresults)
    if cfgRuns['minlevel'] == 0:
        avgcomppct2 = totcomppct2/(numlevels-1)
        avgcomptime2 = totcomptime2/((numlevels-1)*numresults)

    # Decompression
    if cfgConfig['skipdecomp']:
        avgdecomptime, avgdecompstr, totdecompstr = [''] * 3
        avgdecomptime2, avgdecompstr2, totdecompstr2 = [''] * 3
    else:
        avgdecomptime = totdecomptime/(numlevels*numresults)
        avgdecompstr = f"{avgdecomptime:.3f}"
        totdecompstr = f"{totdecomptime:.3f}"
        if cfgRuns['minlevel'] == 0:
            avgdecomptime2 = totdecomptime2/((numlevels-1)*numresults)
            avgdecompstr2 = f"{avgdecomptime2:.3f}"
            totdecompstr2 = f"{totdecomptime2:.3f}"

    # Print totals
    print(f"\n {'avg1':5} {avgcomppct:7.3f}% {avgcomptime:20.3f} {avgdecompstr:>23}")
    if cfgRuns['minlevel'] == 0:
        print(f" {'avg2':5} {avgcomppct2:7.3f}% {avgcomptime2:20.3f} {avgdecompstr2:>23}")
    print(f" {'tot':5} {'':8} {totcomptime:20.3f} {totdecompstr:>23}")

def printfile(level,filename):
    filesize = os.path.getsize(filename)
    print(f"Level {level}: {filename} {filesize/1024/1024:.1f} MiB / {filesize:,} B")

def benchmain():
    ''' Main benchmarking function '''
    global timefile, compfile, decompfile

    print(f"Tool: {cfgRuns['testtool']}")

    # Prepare tempfiles
    timefile = os.path.join(cfgConfig['temp_path'], 'zlib-time.tmp')
    compfile = os.path.join(cfgConfig['temp_path'], 'zlib-testfil.gz')
    decompfile = os.path.join(cfgConfig['temp_path'], 'zlib-testfil.raw')

    tempfiles = dict()

    # Single testfile, we just reference the same file for every level
    if cfgRuns['testmode'] == 'single':
        tmp_filename = os.path.join(cfgConfig['temp_path'], f"deflatebench.tmp")
        srcfile = findfile(cfgSingle['testfile'])
        shutil.copyfile(srcfile,tmp_filename)
        tmp_hash = hashfile(tmp_filename)
        origsize = os.path.getsize(tmp_filename)
        print(f"Activated single file mode")
        printfile(f"{cfgRuns['minlevel']}-{cfgRuns['maxlevel']}", srcfile)

        for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
            tempfiles[level] = dict()
            tempfiles[level]['filename'] = tmp_filename
            tempfiles[level]['hash'] = tmp_hash
            tempfiles[level]['origsize'] = origsize
    else:
        # Multiple testfiles
        if cfgRuns['testmode'] == 'multi':
            print(f"Activated multiple file mode.")
        else:
            print(f"Activated multiple generated file mode. Source: {cfgGen['srcFile']}")

        for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
            tempfiles[level] = dict()
            tmp_filename = os.path.join(cfgConfig['temp_path'], f"deflatebench-{level}.tmp")
            tempfiles[level]['filename'] = tmp_filename

            if cfgRuns['testmode'] == 'multi':
                srcfile = findfile(cfgMulti[level])
                shutil.copyfile(srcfile,tmp_filename)
                printfile(f"{level}", srcfile)
            else:
                generate_testfile(findfile(cfgGen['srcFile']),tmp_filename,cfgGen[level])
                printfile(f"{level}", tmp_filename)

            tempfiles[level]['hash'] = hashfile(tmp_filename)
            tempfiles[level]['origsize'] = os.path.getsize(tmp_filename)

    # Tweak system to reduce benchmark variance
    cputweak(True)

    # Prepare multilevel results array
    results = dict()
    for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
        results[level] = []

    # Run tests and record results
    for run in range(1,cfgRuns['runs']+1):
        if run != 1:
            cfgConfig['skipverify'] = True

        print(f"Starting run {run} of {cfgRuns['runs']}")
        for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
            compsize,comptime,decomptime,hashfail = runtest(tempfiles,level)
            if hashfail != 0:
                print(f"ERROR: level {level} failed crc checking")
            results[level].append( [compsize,comptime,decomptime] )

    printreport(results, tempfiles)

    # Disable system tweaks to restore normal powersaving, turbo, etc
    cputweak(False)

    # Clean up tempfiles
    for level in map(str, range(cfgRuns['minlevel'],cfgRuns['maxlevel']+1)):
        if os.path.isfile(tempfiles[level]['filename']):
            os.unlink(tempfiles[level]['filename'])

def main():
    ''' Main function handles command-line arguments and loading the correct config '''
    global homedir,cfgRuns,cfgConfig,cfgTuning,cfgGen,cfgSingle,cfgMulti

    parser = argparse.ArgumentParser(description='deflatebench - A zlib-ng benchmarking utility. Please see config file for more options.')
    parser.add_argument('-r','--runs', help='Number of benchmark runs.', type=int)
    parser.add_argument('-t','--trimworst', help='Trim the N worst runs per level.', type=int)
    parser.add_argument('-p','--profile', help='Load config profile from config file: ~/deflatebench-[PROFILE].conf')
    parser.add_argument('--write-config', help='Write default configfile to ~/deflatebench.conf.', action='store_true')
    parser.add_argument('-s','--single', help='Activate testmode "Single"', action='store_true')
    parser.add_argument('-m','--multi', help='Activate testmode "Multi".', action='store_true')
    parser.add_argument('-g','--gen', help='Activate testmode "Generate".', action='store_true')
    parser.add_argument('-z','--minigzip', help='Use minigzip for testing.', action='store_true')
    parser.add_argument('-d','--minideflate', help='Use minideflate for testing.', action='store_true')
    parser.add_argument('-l','--testtool', help='Path to test tool', action='store', default=None)
    parser.add_argument('--skipdecomp', help='Skip decompression benchmarks.', action='store_true')
    parser.add_argument('--skipverify', help='Skip verifying compressed files with system gzip.', action='store_true')
    args = parser.parse_args()

    defconfig_path = findfile('deflatebench.conf',fatal=False)

    # Write default config file
    if args.write_config:
        if not defconfig_path:
            defconfig_path = os.path.join( os.path.expanduser("~"), 'deflatebench.conf')
            writeconfig(defconfig_path)
        else:
            print(f"ERROR: {defconfig_path} already exists, not overwriting.")
        sys.exit(1)


    # Load defconfig, then potentially override with values from config file
    cfg = defconfig()
    if args.profile and not args.profile == 'default':
        profilename = f"deflatebench-{args.profile}.conf"
        profilefile = findfile(profilename, fatal=True)
        cfgtmp = parseconfig(profilefile)
        cfg = mergeconfig(cfg,cfgtmp)
        print(f"Loaded config file '{profilefile}'.")
    elif defconfig_path:
        cfgtmp = parseconfig(defconfig_path)
        cfg = mergeconfig(cfg,cfgtmp)
        print(f"Loaded config file '{defconfig_path}'.")
    else:
        print("Loaded default config.")

    # Split config into separate dicts
    cfgRuns = cfg['Testruns']
    cfgConfig = cfg['Config']
    cfgTuning = cfg['Tuning']
    cfgGen = cfg['Testdata_Gen']
    cfgSingle = cfg['Testdata_Single']
    cfgMulti = cfg['Testdata_Multi']

    # Handle commandline parameters
    if args.runs:
        cfgRuns['runs'] = args.runs

    if args.trimworst:
        cfgRuns['trimworst'] = args.trimworst

    if cfgRuns['runs'] <= cfgRuns['trimworst']:
        print("Error, parameter 'runs' needs to be higher than parameter 'trimworst'")
        sys.exit(1)

    if args.single:
        cfgRuns['testmode'] = 'single'
        if args.multi or args.gen:
            print("Error, parameter '--single' conflicts with parameters '--multi' and '--gen'")
            sys.exit(1)

    if args.multi:
        cfgRuns['testmode'] = 'multi'
        if args.single or args.gen:
            print("Error, parameter '--multi' conflicts with parameters '--single' and '--gen'")
            sys.exit(1)

    if args.gen:
        cfgRuns['testmode'] = 'gen'
        if args.single or args.multi:
            print("Error, parameter '--gen' conflicts with parameters '--single' and '--multi'")
            sys.exit(1)

    if args.minigzip:
        if args.minideflate:
            print("Error, parameter '--minigzip' conflicts with parameter '--minideflate'")
            sys.exit(1)
        cfgRuns['testtool'] = 'minigzip'

    if args.minideflate:
        if args.minigzip:
            print("Error, parameter '--minideflate' conflicts with parameter '--minigzip'")
            sys.exit(1)
        cfgRuns['testtool'] = 'minideflate'

    if args.testtool:
        cfgRuns['testtool'] = args.testtool

    if 'minigzip' not in cfgRuns['testtool'] and 'minideflate' not in cfgRuns['testtool']:
        print("Error, config file spesifies invalid testtool. Valid choices are 'minigzip' and 'minideflate'.")
        sys.exit(1)

    if not os.path.isfile( os.path.join( os.getcwd(), cfgRuns['testtool']) ):
        print(f"Error, unable to find '{cfgRuns['testtool']}' in current directory, did you forget to compile?")
        sys.exit(1)

    if args.skipdecomp:
        cfgConfig['skipdecomp'] = True

    if args.skipverify:
        cfgConfig['skipverify'] = True

    # Run main benchmarking function
    benchmain()
main()
