#!/bin/bash
# ON-DEMAND controlled stop of one wave (coordinator order). Same script as the armed guard, with the reason recorded.
#   stop_now.sh <vm> "<reason>"
VM=$1; REASON=$2; LOG=/Users/kitts/archductor/snap-runs/deadline_guard-$VM.log
[ -n "$REASON" ] || { echo "usage: stop_now.sh <vm> <reason>"; exit 2; }
echo "$(date -u +%FT%TZ) ON-DEMAND stop of $VM: $REASON" >> $LOG
gcloud compute scp /Users/kitts/archductor/snap-runs/deadline_stop.sh $VM:/tmp/deadline_stop.sh --zone us-central1-b --project hexera-dev >> $LOG 2>&1 || exit 1
gcloud compute ssh $VM --zone us-central1-b --project hexera-dev --command "bash /tmp/deadline_stop.sh \"$REASON\"" >> $LOG 2>&1
echo "$(date -u +%FT%TZ) deadline_stop.sh rc=$? on $VM" >> $LOG
