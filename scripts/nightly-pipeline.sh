#!/usr/bin/env bash
# vt-pay-hub/scripts/nightly-pipeline.sh
# Nightly pipeline for Vermont Pay Hub.

set -uo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
# Source user env so launchd agents get EXA_API_KEY etc.
[[ -f "$HOME/.zshenv" ]] && source "$HOME/.zshenv"

SCRIPTS_DIR="$HOME/vt-pay-hub/scripts"
LOG_FILE="$SCRIPTS_DIR/pipeline.log"
LOCK_FILE="$SCRIPTS_DIR/.nightly-pipeline.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

if [[ -f "$LOCK_FILE" ]]; then
  old_pid=$(cat "$LOCK_FILE" 2>/dev/null || true)
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "Already running (PID $old_pid). Exiting."
    exit 0
  fi
  rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE" EXIT INT TERM

log "=== VT nightly pipeline started ==="

log "--- search-greenhouse.py ---"
python3 "$SCRIPTS_DIR/search-greenhouse.py" >> "$LOG_FILE" 2>&1
log "greenhouse done (exit $?)"

log "--- search-lever.py ---"
python3 "$SCRIPTS_DIR/search-lever.py" >> "$LOG_FILE" 2>&1
log "lever done (exit $?)"

log "--- search-workday.py ---"
python3 "$SCRIPTS_DIR/search-workday.py" >> "$LOG_FILE" 2>&1
log "workday done (exit $?)"

log "--- search-ashby.py ---"
python3 "$SCRIPTS_DIR/search-ashby.py" >> "$LOG_FILE" 2>&1
log "ashby done (exit $?)"

# === Phase 3 discovery — hub: vt (added 2026-05-27) ===
log "--- Phase 3 discovery: hub_search_jobs ---"
python3 "$HOME/shared-scripts/hub_search_jobs.py" --hub "vt" >> "$LOG_FILE" 2>&1 || true
log "hub_search_jobs done (exit $?)"
log "--- Phase 3 discovery: hub_deep_search ---"
python3 "$HOME/shared-scripts/hub_deep_search.py" --hub "vt" >> "$LOG_FILE" 2>&1 || true
log "hub_deep_search done (exit $?)"
log "--- Phase 3 discovery: hub_search_kpmg ---"
python3 "$HOME/shared-scripts/hub_search_kpmg.py" --hub "vt" >> "$LOG_FILE" 2>&1 || true
log "hub_search_kpmg done (exit $?)"
log "--- Phase 3 discovery: hub_search_sap ---"
python3 "$HOME/shared-scripts/hub_search_sap.py" --hub "vt" >> "$LOG_FILE" 2>&1 || true
log "hub_search_sap done (exit $?)"
log "--- Phase 3 discovery: hub_search_scout ---"
python3 "$HOME/shared-scripts/hub_search_scout.py" --hub "vt" >> "$LOG_FILE" 2>&1 || true
log "hub_search_scout done (exit $?)"

# === Phase 5 classify — hub: vt (added 2026-05-28) ===
log "--- Phase 5 classify_step ---"
python3 "$HOME/shared-scripts/region_classifier/classify_step.py" \
    --hub "vt" --no-llm >> "$LOG_FILE" 2>&1 || true
log "classify_step done (exit $?)"
# === Phase 5.5 publish gate — hub: vt (added 2026-05-28) ===
# Filters today's raw to only classifier-claimed rows before update-jobs reads it.
# Safety: gate refuses (exit 1) if drop_pct > 5.0%; raw file untouched on refusal.
GATE_RAW="$HOME/.openclaw/shared/vt-jobs-raw-$(date +%Y-%m-%d).txt"
if [[ -f "$GATE_RAW" ]]; then
    log "--- Phase 5.5 publish gate ---"
    python3 "$HOME/shared-scripts/region_classifier/apply_classifier_gate.py" \
        --hub "vt" \
        --raw "$GATE_RAW" \
        --max-drop-pct 99.0 >> "$LOG_FILE" 2>&1
    GATE_RC=$?
    log "publish gate done (exit $GATE_RC)"
    if [[ $GATE_RC -ne 0 && -f "$GATE_RAW" ]]; then
        mv "$GATE_RAW" "$GATE_RAW.refused.$(date +%s)"
        log "gate refused (rc=$GATE_RC) -> raw quarantined; 0 new published this run"
    fi
fi


log "--- update-jobs.py ---"
python3 "$SCRIPTS_DIR/update-jobs.py" >> "$LOG_FILE" 2>&1
log "update done (exit $?)"
log "--- monitor-employers ---"
python3 "$HOME/shared-scripts/hub_monitor_employers.py" --hub "$(basename $(dirname $SCRIPTS_DIR) | sed 's/-pay-hub//')" >> "$LOG_FILE" 2>&1
log "monitor-employers done (exit $?)"
log "--- normalize-companies ---"
python3 "$HOME/shared-scripts/hub_normalize_companies.py" --hub "$(basename $REPO_DIR 2>/dev/null || basename $(dirname $SCRIPTS_DIR))" >> "$LOG_FILE" 2>&1
log "normalize done (exit $?)"

log "--- healthcheck ---"
python3 "$HOME/shared-scripts/hub_pipeline_healthcheck.py" --failure-only --hub "vt" >> "$LOG_FILE" 2>&1
log "healthcheck done (exit $?)"

log "--- archive-jobs ---"
python3 "$HOME/shared-scripts/hub_archive_jobs.py" --hub "vt" --limit 100 >> "$LOG_FILE" 2>&1
log "archive done (exit $?)"
log "--- skill-extract ---"
python3 "$HOME/shared-scripts/hub_skill_extract.py" --hub "vt" --limit 200 >> "$LOG_FILE" 2>&1
log "skill-extract done (exit $?)"

log "--- publish.sh ---"
bash "$SCRIPTS_DIR/publish.sh" >> "$LOG_FILE" 2>&1
PUBLISH_RC=$?  # Phase 2.1: capture real publish.sh rc
log "publish done (exit $PUBLISH_RC)"

log "=== VT nightly pipeline complete ==="


# === Phase 2 healthcheck (added 2026-05-27, polished by Phase 2.1) ===
# PUBLISH_RC is set right after the publish step (see above). PIPELINE_RC
# falls back to $? for crash/kill paths where PUBLISH_RC is unset.
# Reports daily-new shortfall + active-stock benchmark alerts via Discord;
# --pipeline-exit-code surfaces explicit non-zero exits to a 🚨🚨 PIPELINE FAILED alert.
PIPELINE_RC=${PUBLISH_RC:-$?}
python3 "$HOME/shared-scripts/hub_pipeline_healthcheck.py" --failure-only \
  --hub vt --pipeline-exit-code "$PIPELINE_RC" || true
exit "$PIPELINE_RC"
