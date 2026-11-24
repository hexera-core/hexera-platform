#!/bin/bash
# Coordinator-approved: at <deadline UTC>, if the wave on <vm> has not finished (no out/ALLDONE, no out/RUN_DONE),
# run deadline_stop.sh on it. If the wave finished first, do nothing. Logs to deadline_guard-<vm>.log.
#   deadline_guard.sh <vm> <YYYY-MM-DDTHH:MM:SSZ>
VM=$1; AT=$2; LOG=/Users/kitts/archductor/snap-runs/deadline_guard-$VM.log
ZONE=us-central1-b; PROJECT=hexera-dev
at_s=$(python3 -I -c "from datetime import datetime;print(int(datetime.fromisoformat('$AT'.replace('Z','+00:00')).timestamp()))")
echo "$(date -u +%FT%TZ) armed for $VM at $AT" >> $LOG
while [ "$(date +%s)" -lt "$at_s" ]; do
  st=$(gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command 'ls ~/snap/out/ALLDONE ~/snap/out/RUN_DONE 2>/dev/null | wc -l' 2>/dev/null | tail -1)
  if [ -n "$st" ] && [ "$st" -gt 0 ]; then echo "$(date -u +%FT%TZ) $VM finished before its deadline: guard stands down" >> $LOG; exit 0; fi
  if ! gcloud compute instances describe $VM --zone $ZONE --project $PROJECT --format='value(status)' >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) $VM no longer exists: guard stands down" >> $LOG; exit 0; fi
  sleep 120
done
st=$(gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command 'ls ~/snap/out/ALLDONE ~/snap/out/RUN_DONE 2>/dev/null | wc -l' 2>/dev/null | tail -1)
if [ -n "$st" ] && [ "$st" -gt 0 ]; then echo "$(date -u +%FT%TZ) $VM finished at the deadline: guard stands down" >> $LOG; exit 0; fi
echo "$(date -u +%FT%TZ) DEADLINE reached, $VM not finished: running deadline_stop.sh" >> $LOG
gcloud compute scp /Users/kitts/archductor/snap-runs/deadline_stop.sh $VM:/tmp/deadline_stop.sh --zone $ZONE --project $PROJECT >> $LOG 2>&1
gcloud compute ssh $VM --zone $ZONE --project $PROJECT --command 'bash /tmp/deadline_stop.sh' >> $LOG 2>&1
echo "$(date -u +%FT%TZ) deadline_stop.sh rc=$? on $VM" >> $LOG
