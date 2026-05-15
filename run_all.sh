#!/bin/zsh
# Run all lead-gen scrapers sequentially, then refresh Hot Leads.
# Usage: ./run_all.sh

set -o pipefail
cd "$(dirname "$0")"

PYTHON=".venv/bin/python3"
LOG_FILE="logs/run_all_$(date +%Y%m%d_%H%M%S).log"
mkdir -p logs

scrapers=(
    "Competitor Intel|src.scrapers.competitor_intel"
    "News|src.scrapers.news_aggregator"
    "FDA Warning Letters|src.scrapers.fda_warning_letters"
    "Clinical Trials|src.scrapers.clinical_trials"
    "FDA Recalls|src.scrapers.fda_recalls"
    "SEC Form D|src.scrapers.sec_form_d"
    "Funding News|src.scrapers.funding_news"
    "MHRA EMA|src.scrapers.mhra_ema"
    "FDA Inspections|src.scrapers.fda_inspections"
    "Gov Contracts|src.scrapers.gov_contracts"
)

declare -a names statuses timings

run_one() {
    local name="$1" module="$2"
    local start=$SECONDS
    printf "\n[%s] Starting: %s\n" "$(date +%H:%M:%S)" "$name"
    $PYTHON -m "$module" 2>&1
    local rc=$?
    local elapsed=$(( SECONDS - start ))
    if [[ $rc -eq 0 ]]; then
        statuses+=("OK")
        printf "[%s] %s -- OK (%ds)\n" "$(date +%H:%M:%S)" "$name" "$elapsed"
    else
        statuses+=("FAILED")
        printf "[%s] %s -- FAILED exit %d (%ds)\n" "$(date +%H:%M:%S)" "$name" "$rc" "$elapsed"
    fi
    names+=("$name")
    timings+=("${elapsed}s")
}

echo "=== Lead-Gen Full Run — $(date '+%Y-%m-%d %H:%M') ===" | tee "$LOG_FILE"

for entry in "${scrapers[@]}"; do
    IFS='|' read -r name module <<< "$entry"
    run_one "$name" "$module"
done | tee -a "$LOG_FILE"

# Hot Leads aggregator
printf "\n[%s] Starting: Hot Leads Aggregator\n" "$(date +%H:%M:%S)" | tee -a "$LOG_FILE"
start=$SECONDS
$PYTHON -m src.aggregator.hot_leads 2>&1 | tee -a "$LOG_FILE"
rc=$?
elapsed=$(( SECONDS - start ))
names+=("Hot Leads")
if [[ $rc -eq 0 ]]; then
    statuses+=("OK")
else
    statuses+=("FAILED")
fi
timings+=("${elapsed}s")

# Summary table
{
    printf "\n%s\n" "============================================================"
    printf "  SUMMARY — %s\n" "$(date '+%Y-%m-%d %H:%M')"
    printf "%s\n" "============================================================"
    printf "  %-25s %-10s %8s\n" "Scraper" "Status" "Time"
    printf "  %-25s %-10s %8s\n" "-------" "------" "----"
    for i in {1..${#names[@]}}; do
        printf "  %-25s %-10s %8s\n" "${names[$i]}" "${statuses[$i]}" "${timings[$i]}"
    done
    printf "%s\n" "============================================================"
} | tee -a "$LOG_FILE"

echo "Full log: $LOG_FILE"
