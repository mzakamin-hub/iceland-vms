#!/bin/sh
# gvm-drmtrace.sh SECONDS CMD... - (in the guest, as root) run CMD for SECONDS and record why its frames do or do
# not reach the host (gvm.sh V6-13 / V6-14 / V6-15).
#   v2 (24 Oct 2026): the v1 drm.debug trace overflowed the guest's 128 KiB log buffer (its counts were from the
#   last ~1 s only). Now, on a guest kernel with ftrace (Image-trace / Image-evnow): hit counts of the KMS / vblank /
#   fence / virtio-gpu functions (function profiler), a function timeline, the drm vblank + dma_fence events, the
#   client's kernel stacks and the blocked tasks (sysrq w) half way through, and the DRM atomic state. On a kernel
#   without ftrace: drm.debug into the log as before (marked NO-FTRACE).
# Output: a summary on stdout (FN <function> <hits> lines, EV <event> <count>, STACK lines), every file in
# /tmp/gvm-drmtrace/ (gvm.sh copies it as a tarball).
secs=$1; shift
O=/tmp/gvm-drmtrace; rm -rf $O; mkdir -p $O
T=/sys/kernel/tracing
[ -d $T/events ] || mount -t tracefs nodev $T 2>/dev/null
[ -d /sys/kernel/debug/dri ] || mount -t debugfs nodev /sys/kernel/debug 2>/dev/null
FNS="drm_mode_atomic_ioctl drm_mode_page_flip_ioctl drm_mode_dirtyfb_ioctl drm_mode_setcrtc
drm_atomic_helper_commit drm_atomic_helper_setup_commit commit_work commit_tail drm_atomic_helper_commit_tail
drm_atomic_helper_wait_for_fences drm_atomic_helper_wait_for_dependencies drm_atomic_helper_wait_for_flip_done
drm_atomic_helper_commit_hw_done drm_atomic_helper_commit_planes drm_atomic_helper_commit_modeset_enables
virtio_gpu_plane_prepare_fb virtio_gpu_primary_plane_update virtio_gpu_crtc_atomic_flush virtio_gpu_crtc_atomic_enable
virtio_gpu_cmd_set_scanout virtio_gpu_cmd_set_scanout_blob virtio_gpu_cmd_resource_flush virtio_gpu_cmd_transfer_to_host_2d
drm_crtc_vblank_get drm_crtc_arm_vblank_event drm_crtc_send_vblank_event drm_crtc_handle_vblank drm_handle_vblank
drm_vblank_timer_function drm_crtc_vblank_start_timer drm_crtc_vblank_cancel_timer drm_crtc_vblank_on drm_crtc_vblank_off
drm_calc_timestamping_constants dma_fence_default_wait dma_fence_wait_timeout virtio_gpu_fence_event_process
virtio_gpu_execbuffer_ioctl"
ftrace=0
if [ -w $T/set_ftrace_filter ] && [ -e $T/function_profile_enabled ]; then
	ftrace=1
	echo 0 > $T/tracing_on; echo nop > $T/current_tracer; echo > $T/trace
	echo 16384 > $T/buffer_size_kb
	: > $T/set_ftrace_filter
	for f in $FNS; do echo "$f" >> $T/set_ftrace_filter 2>/dev/null && echo "$f" >> $O/fn-traced.txt || echo "$f" >> $O/fn-not-traceable.txt; done
	for e in drm/drm_vblank_event drm/drm_vblank_event_queued drm/drm_vblank_event_delivered \
		dma_fence/dma_fence_emit dma_fence/dma_fence_signaled dma_fence/dma_fence_wait_start dma_fence/dma_fence_wait_end \
		virtio_gpu/virtio_gpu_cmd_queue virtio_gpu/virtio_gpu_cmd_response; do
		[ -e $T/events/$e/enable ] && echo 1 > $T/events/$e/enable && echo "$e" >> $O/events.txt
	done
	echo 0 > $T/function_profile_enabled; echo 1 > $T/function_profile_enabled
	echo function > $T/current_tracer; echo 1 > $T/tracing_on
else
	echo "NO-FTRACE (this guest kernel has no function tracer: drm.debug instead)"
	p=/sys/module/drm/parameters/debug; old=$(cat $p 2>/dev/null || echo 0); echo 0x16 > $p
fi
dmesg -C
cat /sys/kernel/debug/dri/0/state > $O/dri-state-before.txt 2>&1
# v10f: kmscube quits ("user interrupted!") as soon as its stdin is readable; an ssh stdin at EOF is. A pipe
# from sleep never becomes readable while the client runs
sleep $((secs + 10)) | "$@" > $O/cmd.txt 2>&1 &
pid=$!
sleep $((secs / 2))
# half way: where is the client (and every kernel thread blocked on it)?
for t in $(pgrep -P $pid) $pid $(pgrep -f '^(weston|kmscube|Xorg|labwc)' 2>/dev/null); do
	for s in /proc/$t/task/*/stack; do [ -r "$s" ] && { echo "== $s $(cat /proc/$t/comm 2>/dev/null)"; cat "$s"; }; done
done > $O/stacks.txt 2>&1
ps -eLo pid,tid,stat,wchan:32,comm > $O/ps-mid.txt 2>&1   # v10c: v10b's kmscube stacks came back empty
for t in $(pgrep -x kmscube) $(pgrep -x weston); do echo "== $t syscall $(cat /proc/$t/syscall 2>&1) wchan $(cat /proc/$t/wchan 2>&1)"; done >> $O/stacks.txt
echo w > /proc/sysrq-trigger 2>/dev/null
cat /sys/kernel/debug/dri/0/state > $O/dri-state-mid.txt 2>&1
sleep $((secs - secs / 2))
kill $pid 2>/dev/null; sleep 1; kill -9 $pid 2>/dev/null; pkill -9 -x weston 2>/dev/null; pkill -9 -x kmscube 2>/dev/null
if [ $ftrace = 1 ]; then
	echo 0 > $T/tracing_on; echo 0 > $T/function_profile_enabled
	cat $T/trace > $O/trace.txt
	cat $T/trace_stat/function* 2>/dev/null > $O/profile.txt
	echo nop > $T/current_tracer; : > $T/set_ftrace_filter
	for e in $(cat $O/events.txt 2>/dev/null); do echo 0 > $T/events/$e/enable; done
else
	echo "$old" > $p
fi
dmesg > $O/dmesg.txt
echo "CMD-RC $(wait $pid 2>/dev/null; echo $?)"
if [ $ftrace = 1 ]; then
	awk '$2 ~ /^[0-9]+$/ && $1 !~ /Function|---/ {h[$1] += $2} END {for (f in h) print "FN", f, h[f]}' $O/profile.txt | sort
	for e in $(cat $O/events.txt 2>/dev/null); do echo "EV ${e#*/} $(grep -c " ${e#*/}:" $O/trace.txt)"; done
	echo "NOT-TRACEABLE $(tr '\n' ' ' < $O/fn-not-traceable.txt 2>/dev/null)"
	echo "TRACE-LINES $(grep -vc '^#' $O/trace.txt) (first $(grep -v '^#' $O/trace.txt | head -1 | awk '{print $4}') last $(tail -1 $O/trace.txt | awk '{print $4}'))"
fi
echo "ERRORS:"
grep -iE 'timed out|flip_done|hw_done|fence|error|fail|dotclock|WARNING' $O/dmesg.txt | grep -v 'sysrq\|Show Blocked' | head -n 12
echo "CMD:"
grep -iE 'fence|modifier|format|atomic|error|fail' $O/cmd.txt | head -n 8
echo "STACKS:"
grep -E '^==|drm_|virtio_gpu|dma_fence|wait' $O/stacks.txt | head -n 30
echo "BLOCKED:"
sed -n '/Show Blocked State/,$p' $O/dmesg.txt | grep -E 'task:|drm_|virtio_gpu|dma_fence|commit' | head -n 20
