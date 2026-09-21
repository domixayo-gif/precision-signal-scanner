import os
import time
import json
from iqoptionapi.stable_api import IQ_Option


EMAIL = os.getenv("IQ_EMAIL", "").strip()
PASSWORD = os.getenv("IQ_PASSWORD", "").strip()


print("=" * 70)
print("IQ OPTION OTC DIAGNOSTIC")
print("=" * 70)


if not EMAIL or not PASSWORD:
    print("ERROR: IQ_EMAIL or IQ_PASSWORD is missing.")
    raise SystemExit


api = IQ_Option(EMAIL, PASSWORD)

print("\n[1] Connecting...")

check, reason = api.connect()

print("Connected:", check)
print("Reason:", reason)

if not check:
    raise SystemExit


print("\n[2] Connection status:")

try:
    print(api.check_connect())
except Exception as e:
    print("check_connect error:", repr(e))


print("\n[3] Getting all open time...")

try:
    data = api.get_all_open_time()

    print("Response type:", type(data).__name__)

except Exception as e:
    print("get_all_open_time ERROR:")
    print(repr(e))
    raise SystemExit


print("\n[4] TOP LEVEL DATA")
print("-" * 70)

if isinstance(data, dict):

    for category, value in data.items():

        if isinstance(value, dict):

            print(
                f"{category}: "
                f"{len(value)} instruments"
            )

        else:

            print(
                f"{category}: "
                f"{type(value).__name__}"
            )

else:

    print("Unexpected response:")
    print(type(data))


print("\n[5] ALL OTC INSTRUMENTS")
print("-" * 70)

otc_found = 0

if isinstance(data, dict):

    for category, instruments in data.items():

        if not isinstance(instruments, dict):
            continue

        for name, info in instruments.items():

            if "OTC" in str(name).upper():

                otc_found += 1

                print(
                    f"{category:<10} "
                    f"{name:<25} "
                    f"{info}"
                )


print("-" * 70)

print(
    "TOTAL OTC INSTRUMENTS FOUND:",
    otc_found
)


print("\n[6] CHECK COMMON OTC PAIRS")
print("-" * 70)

test_pairs = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "GBPJPY-OTC",
    "AUDCAD-OTC",
    "NZDUSD-OTC",
    "USDCHF-OTC",
]


for pair in test_pairs:

    print("\nPAIR:", pair)

    found = False

    if isinstance(data, dict):

        for category, instruments in data.items():

            if not isinstance(instruments, dict):
                continue

            if pair in instruments:

                found = True

                print(
                    "  Category:",
                    category
                )

                print(
                    "  Data:",
                    instruments[pair]
                )

    if not found:
        print("  NOT FOUND")


print("\n[7] ACTIVE OPCODES")
print("-" * 70)

try:

    opcodes = api.get_all_ACTIVES_OPCODE()

    print(
        "Total opcode entries:",
        len(opcodes)
    )

    otc_opcodes = {
        name: code
        for name, code in opcodes.items()
        if "OTC" in str(name).upper()
    }

    print(
        "OTC opcode entries:",
        len(otc_opcodes)
    )

    for name, code in otc_opcodes.items():

        print(
            f"{name:<25} -> {code}"
        )

except Exception as e:

    print(
        "Opcode check error:",
        repr(e)
    )


print("\n[8] DIRECT CANDLE TEST")
print("-" * 70)

for pair in test_pairs:

    try:

        candles = api.get_candles(
            pair,
            60,
            10,
            time.time()
        )

        count = len(candles) if candles else 0

        print(
            f"{pair:<20} "
            f"{count} candles"
        )

    except Exception as e:

        print(
            f"{pair:<20} ERROR: {repr(e)}"
        )


print("\n" + "=" * 70)
print("DIAGNOSTIC COMPLETE")
print("=" * 70)

try:
    api.close()
except Exception:
    passpass
