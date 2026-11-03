#!/bin/sh
# R2 remote sampler body (v2 spec D6). Read-only: it only reads /proc, /sys and the
# output of ss / iw / vcgencmd / pgrep, and prints marker lines to stdout.
# It is passed on stdin, so no file is created on the device:
#   ssh <host> sh -s -- <camera_ip> <seconds> <process> <interface> < r2_remote.sh
# Marker lines start with "@"; everything else is raw text of the section above it.
# The Mac side parses the text, so the device does no parsing beyond shell builtins.
ip=$1
seconds=$2
proc=$3
ifc=$4
ticks=$((seconds * 10))
hz=$(getconf CLK_TCK 2>/dev/null)
echo "@hello 1 ${hz:-100}"

dump() {
    [ -r "$1" ] || return 0
    while IFS= read -r line; do
        echo "$line"
    done < "$1"
}

have_iw=0
command -v iw >/dev/null 2>&1 && have_iw=1
have_vc=0
command -v vcgencmd >/dev/null 2>&1 && have_vc=1
pid=
n=0
while [ "$n" -lt "$ticks" ]; do
    read -r up idle < /proc/uptime
    echo "@tick $n $up"
    echo "@ss"
    ss -tin dst "$ip" 2>/dev/null
    if [ -r /proc/stat ]; then
        read -r cpu_line < /proc/stat
        echo "@stat"
        echo "$cpu_line"
    fi
    echo "@psi"
    dump /proc/pressure/cpu
    if [ -r /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ]; then
        read -r freq < /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
        echo "@freq $freq"
    fi
    if [ -n "$pid" ]; then
        echo "@sched $pid"
        for f in /proc/$pid/task/*/schedstat; do
            [ -r "$f" ] || continue
            tid=${f#/proc/$pid/task/}
            tid=${tid%/schedstat}
            read -r run_ns wait_ns slices < "$f"
            echo "$tid $run_ns $wait_ns $slices"
        done
    fi
    if [ $((n % 10)) -eq 0 ]; then
        pid=$(pgrep -o -x "$proc" 2>/dev/null)
        echo "@snmp"
        dump /proc/net/snmp
        echo "@netstat"
        dump /proc/net/netstat
        echo "@wireless"
        dump /proc/net/wireless
        if [ "$have_iw" -eq 1 ]; then
            echo "@iw"
            iw dev "$ifc" station dump 2>/dev/null
        fi
        if [ "$have_vc" -eq 1 ]; then
            echo "@throttled"
            vcgencmd get_throttled 2>/dev/null
        fi
        read -r self_stat < /proc/self/stat
        echo "@self"
        echo "$self_stat"
    fi
    n=$((n + 1))
    sleep 0.1
done
echo "@end $n"
