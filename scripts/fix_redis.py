"""
Clear stale Redis lock and orphaned backup:* keys.
Run this when a backup got stuck and new ones are being skipped.
"""
import redis

REDIS_URL = "redis://default:uUbyPQDRSSdjOrAvVmEFxSiHKlrfoUSF@shinkansen.proxy.rlwy.net:41027"

r = redis.from_url(REDIS_URL, decode_responses=True)

print("=== Redis fix script ===\n")

# 1. Show current lock state
lock = r.get("backup:global_lock")
if lock:
    print(f"Stale lock found: backup:global_lock = {lock}")
    r.delete("backup:global_lock")
    print("-> Lock deleted.\n")
else:
    print("No active lock found.\n")

# 2. Show and clear orphaned backup:progress and backup:phase keys
progress_keys = r.keys("backup:progress:*")
phase_keys    = r.keys("backup:phase:*")

if progress_keys or phase_keys:
    print(f"Orphaned progress keys ({len(progress_keys)}):")
    for k in progress_keys:
        print(f"  {k} = {r.get(k)}")

    print(f"\nOrphaned phase keys ({len(phase_keys)}):")
    for k in phase_keys:
        print(f"  {k} = {r.get(k)}")

    all_keys = progress_keys + phase_keys
    r.delete(*all_keys)
    print(f"\n-> Deleted {len(all_keys)} orphaned keys.\n")
else:
    print("No orphaned backup keys.\n")

# 3. Final state
print("=== Redis state after fix ===")
remaining = r.keys("backup:*")
if remaining:
    for k in remaining:
        print(f"  {k} = {r.get(k)}")
else:
    print("  Clean — no backup:* keys remain.")
