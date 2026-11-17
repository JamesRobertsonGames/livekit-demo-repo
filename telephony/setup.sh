#!/usr/bin/env bash
# Connect a Twilio phone number to the Shipcheck agent on LiveKit Cloud.
#
#   SHIPCHECK_PHONE_NUMBER=+44XXXXXXXXXX telephony/setup.sh            # LiveKit side
#   SHIPCHECK_PHONE_NUMBER=+44XXXXXXXXXX telephony/setup.sh --twilio   # and the Twilio trunk
#   telephony/setup.sh --status                                        # show what exists
#
# LiveKit side (always): an inbound trunk for your number and a dispatch rule that
# puts each call in its own room and dispatches the "shipcheck" agent to it. Both
# are created from the JSON files next to this script, and the script is safe to
# re-run: anything that already exists by name is left alone.
#
# Twilio side (--twilio, needs the twilio CLI and a logged-in profile): an Elastic
# SIP trunk whose origination URI is your LiveKit SIP endpoint, with your number
# attached. Without --twilio the script prints those commands for you to run.
#
# Nothing here buys a phone number. Buy one in the Twilio console first; UK
# numbers need a regulatory bundle (proof of address), which Twilio walks you
# through. Docs: https://docs.livekit.io/telephony/start/providers/twilio/
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
trunk_json="$here/inbound-trunk.json"
rule_json="$here/dispatch-rule.json"
mode="${1:-}"

command -v lk >/dev/null || { echo "lk (the LiveKit CLI) is not installed: https://docs.livekit.io/reference/developer-tools/livekit-cli/" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# Read a field out of the config files, and look up an id by name in `lk ... list --json`.
json_field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."): d=d[k]
print(d)' "$1" "$2"; }
id_by_name() { python3 -c '
import json, sys
name, keys = sys.argv[1], sys.argv[2:]
def walk(node):
    if isinstance(node, dict):
        if node.get("name") == name:
            for k in keys:
                if node.get(k):
                    print(node[k]); sys.exit(0)
        for v in node.values(): walk(v)
    elif isinstance(node, list):
        for v in node: walk(v)
try:
    walk(json.load(sys.stdin))
except json.JSONDecodeError:
    pass
' "$@"; }

trunk_name="$(json_field "$trunk_json" trunk.name)"
rule_name="$(json_field "$rule_json" dispatch_rule.name)"

project_id="$(lk project list --json | python3 -c '
import json, os, sys
projects = json.load(sys.stdin)
url = os.environ.get("LIVEKIT_URL", "")
match = [p for p in projects if p.get("URL") == url] or projects
print(match[0]["ProjectId"] if match else "")')"
[ -n "$project_id" ] || { echo "No LiveKit project found. Run: lk cloud auth" >&2; exit 1; }
sip_endpoint="${project_id#p_}.sip.livekit.cloud"

existing_trunk="$(lk sip inbound list --json | id_by_name "$trunk_name" sip_trunk_id sipTrunkId)"
existing_rule="$(lk sip dispatch list --json | id_by_name "$rule_name" sip_dispatch_rule_id sipDispatchRuleId)"

if [ "$mode" = "--status" ]; then
  echo "LiveKit project:    $project_id"
  echo "SIP endpoint:       $sip_endpoint"
  echo "Inbound trunk:      ${existing_trunk:-not created} ($trunk_name)"
  echo "Dispatch rule:      ${existing_rule:-not created} ($rule_name)"
  exit 0
fi

number="${SHIPCHECK_PHONE_NUMBER:-}"
[[ "$number" =~ ^\+[1-9][0-9]{7,14}$ ]] || { echo "Set SHIPCHECK_PHONE_NUMBER to your Twilio number in E.164 form, for example +441234567890." >&2; exit 1; }

echo "== LiveKit: inbound trunk"
if [ -n "$existing_trunk" ]; then
  echo "exists: $existing_trunk"
else
  lk sip inbound create --numbers "$number" "$trunk_json"
  existing_trunk="$(lk sip inbound list --json | id_by_name "$trunk_name" sip_trunk_id sipTrunkId)"
  [ -n "$existing_trunk" ] || { echo "could not read back the new trunk id" >&2; exit 1; }
fi

echo "== LiveKit: dispatch rule"
if [ -n "$existing_rule" ]; then
  echo "exists: $existing_rule"
else
  lk sip dispatch create --trunks "$existing_trunk" "$rule_json"
fi

twilio_domain="shipcheck-${project_id#p_}.pstn.twilio.com"
twilio_steps() {
  cat <<STEPS

== Twilio: Elastic SIP trunk (run these with the twilio CLI, or pass --twilio)

  twilio api trunking v1 trunks create \\
    --friendly-name "Shipcheck" --domain-name "$twilio_domain"

  twilio api trunking v1 trunks origination-urls create \\
    --trunk-sid <trunk SID from the previous command> \\
    --friendly-name "LiveKit SIP URI" \\
    --sip-url "sip:$sip_endpoint;transport=tcp" \\
    --weight 1 --priority 1 --enabled

  twilio phone-numbers list        # find the SID of $number
  twilio api trunking v1 trunks phone-numbers create \\
    --trunk-sid <trunk SID> --phone-number-sid <phone number SID>
STEPS
}

if [ "$mode" != "--twilio" ]; then
  twilio_steps
else
  command -v twilio >/dev/null || { echo "twilio CLI not found: https://www.twilio.com/docs/twilio-cli/getting-started/install" >&2; twilio_steps; exit 1; }
  echo "== Twilio: SIP trunk"
  trunk_sid="$(twilio api trunking v1 trunks list -o json | python3 -c '
import json, sys
for t in json.load(sys.stdin):
    if t.get("domainName") == sys.argv[1]: print(t["sid"]); break' "$twilio_domain")"
  if [ -z "$trunk_sid" ]; then
    trunk_sid="$(twilio api trunking v1 trunks create --friendly-name "Shipcheck" --domain-name "$twilio_domain" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["sid"])')"
    twilio api trunking v1 trunks origination-urls create --trunk-sid "$trunk_sid" \
      --friendly-name "LiveKit SIP URI" --sip-url "sip:$sip_endpoint;transport=tcp" \
      --weight 1 --priority 1 --enabled
  fi
  echo "trunk: $trunk_sid"
  number_sid="$(twilio phone-numbers list -o json | python3 -c '
import json, sys
for n in json.load(sys.stdin):
    if n.get("phoneNumber") == sys.argv[1]: print(n["sid"]); break' "$number")"
  [ -n "$number_sid" ] || { echo "$number is not a number on this Twilio account. Buy it in the Twilio console first." >&2; exit 1; }
  twilio api trunking v1 trunks phone-numbers create --trunk-sid "$trunk_sid" --phone-number-sid "$number_sid" || echo "(number may already be attached)"
fi

cat <<DONE

Done. Start the agent so it can take the call:
  lk agent dev            # from your machine, or
  lk agent create         # deployed to LiveKit Cloud (first time; later: lk agent deploy)
Then phone $number.
DONE
