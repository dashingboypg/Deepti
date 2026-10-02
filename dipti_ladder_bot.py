import os
import json
import time
import uuid
import boto3
from decimal import Decimal, ROUND_HALF_UP
from delta_rest_client import DeltaRestClient

# ============================================================
# DIPTI LADDER
# Buy every $1 lower, sell each filled buy $3 higher
# ============================================================

SECRET_NAME = "Dipti"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "ETHUSD"
PRODUCT_ID = 3136

ORDER_SIZE = 1
MAX_POSITION = 45
BUY_STEP = 1.0
TAKE_PROFIT = 3.0

STATE_FILE = "/home/ec2-user/delta-bot/dipti-ladder/dipti_ladder_state.json"
BOT_PREFIX = "DLR-"
POLL_SECONDS = 2


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "|", msg, flush=True)


# ============================================================
# AWS SECRET
# ============================================================

def load_secret():
    sm = boto3.session.Session(
        region_name=AWS_REGION
    ).client("secretsmanager")

    data = json.loads(
        sm.get_secret_value(
            SecretId=SECRET_NAME
        )["SecretString"]
    )

    api_key = data.get("API_Key")
    api_secret = data.get("API_Secret_Key")

    if not api_key or not api_secret:
        raise RuntimeError(
            "API_Key or API_Secret_Key missing from Dipti"
        )

    return api_key, api_secret


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "version": 1,
        "initialized": False,
        "bot_position": 0,
        "next_sequence": 1,
        "orders": {}
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state()

        state.setdefault("version", 1)
        state.setdefault("initialized", False)
        state.setdefault("bot_position", 0)
        state.setdefault("next_sequence", 1)
        state.setdefault("orders", {})

        return state

    except Exception as e:
        log(f"STATE LOAD ERROR: {e}")
        return default_state()


def save_state():
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w") as f:
        json.dump(STATE, f, indent=2)

    os.replace(tmp, STATE_FILE)


# ============================================================
# PRICE
# ============================================================

def round_cmp(price):
    return int(
        Decimal(str(price)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP
        )
    )


def price_string(price):
    if float(price).is_integer():
        return str(int(price))
    return f"{price:.2f}"


# ============================================================
# CLIENT
# ============================================================

API_KEY, API_SECRET = load_secret()

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
    raise_for_status=True
)

STATE = load_state()


# ============================================================
# TICKER
# ============================================================

def get_cmp():
    ticker = client.get_ticker(SYMBOL)

    if isinstance(ticker, dict):
        for key in (
            "close",
            "last_price",
            "mark_price",
            "spot_price"
        ):
            if ticker.get(key) is not None:
                return float(ticker[key])

    raise RuntimeError(
        f"Unable to read CMP: {ticker}"
    )


# ============================================================
# ORDERS
# ============================================================

def new_client_order_id(role):
    seq = STATE["next_sequence"]
    STATE["next_sequence"] = seq + 1

    return f"{BOT_PREFIX}{role}-{seq}-{uuid.uuid4().hex[:6]}"


def get_live_orders():
    orders = client.get_live_orders()

    if isinstance(orders, dict):
        orders = orders.get(
            "result",
            orders.get("orders", [])
        )

    if not isinstance(orders, list):
        return []

    return [
        o for o in orders
        if int(o.get("product_id", -1)) == PRODUCT_ID
    ]


def get_bot_open_orders():
    result = {}

    for order in get_live_orders():
        oid = order.get(
            "client_order_id",
            ""
        )

        if oid.startswith(BOT_PREFIX):
            result[str(order["id"])] = order

    return result


def place_limit(side, price, role):
    client_oid = new_client_order_id(role)

    payload = {
        "product_id": PRODUCT_ID,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_string(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "client_order_id": client_oid
    }

    log(
        f"PLACING {side.upper()} "
        f"{ORDER_SIZE} LOT @ ${price_string(price)}"
    )

    result = client.create_order(payload)

    if not isinstance(result, dict):
        raise RuntimeError(
            f"Unexpected order response: {result}"
        )

    order_id = result.get("id")

    if order_id is None:
        raise RuntimeError(
            f"No order ID returned: {result}"
        )

    STATE["orders"][str(order_id)] = {
        "role": role,
        "side": side,
        "price": float(price),
        "size": ORDER_SIZE,
        "client_order_id": client_oid,
        "handled": False
    }

    save_state()

    log(
        f"ORDER CREATED | ID={order_id} "
        f"| {side.upper()} ${price_string(price)}"
    )


# ============================================================
# STARTUP
# ============================================================

def startup_reconcile():
    live = get_bot_open_orders()

    try:
        position = client.get_position(PRODUCT_ID)
        exchange_position = int(
            float(position.get("size", 0) or 0)
        )
    except Exception as e:
        log(f"POSITION CHECK ERROR: {e}")
        raise

    log(
        f"STARTUP | POSITION={exchange_position} "
        f"| BOT ORDERS={len(live)}"
    )

    if live or exchange_position != 0:
        if STATE.get("initialized"):
            log("EXISTING BOT STATE PRESERVED")
            return

        raise RuntimeError(
            "EXCHANGE HAS EXISTING POSITION/ORDERS "
            "BUT LOCAL STATE IS NOT INITIALIZED. "
            "STOPPING FOR SAFETY."
        )

    if STATE.get("initialized"):
        log(
            "STALE STATE DETECTED | "
            "ZERO POSITION + ZERO BOT ORDERS"
        )

    STATE["initialized"] = False
    STATE["bot_position"] = 0
    STATE["orders"] = {}

    save_state()


def initialize_grid():
    if STATE["initialized"]:
        return

    cmp = get_cmp()
    anchor = round_cmp(cmp)

    levels = MAX_POSITION // ORDER_SIZE

    log("==========================================")
    log("DIPTI ETHUSD LADDER")
    log("==========================================")
    log(f"CMP: ${cmp}")
    log(f"ROUNDED ANCHOR: ${anchor}")
    log(f"BUY STEP: ${BUY_STEP}")
    log(f"TAKE PROFIT: ${TAKE_PROFIT}")
    log(f"ORDER SIZE: {ORDER_SIZE} LOT")
    log(f"MAX POSITION: {MAX_POSITION} LOTS")
    log(f"INITIAL BUY ORDERS: {levels}")
    log("==========================================")

    for level in range(levels):
        price = anchor - (
            level * BUY_STEP
        )

        place_limit(
            "buy",
            price,
            "B"
        )

        time.sleep(0.15)

    STATE["initialized"] = True
    save_state()

    log(
        f"INITIAL GRID READY | "
        f"{levels} BUY ORDERS"
    )


# ============================================================
# FILLS
# ============================================================

def get_order_status(order_id):
    try:
        return client.get_order_by_id(
            int(order_id)
        )
    except Exception as e:
        log(
            f"ORDER STATUS ERROR "
            f"{order_id}: {e}"
        )
        return None


def handle_buy_fill(order_id, info, order):
    buy_price = float(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"]
            )
        )
    )

    STATE["bot_position"] += info["size"]

    sell_price = buy_price + TAKE_PROFIT

    log(
        f"BUY FILLED | "
        f"${price_string(buy_price)} "
        f"| POSITION={STATE['bot_position']} "
        f"| TP=${price_string(sell_price)}"
    )

    place_limit(
        "sell",
        sell_price,
        "S"
    )

    info["handled"] = True
    info["fill_price"] = buy_price

    save_state()


def handle_sell_fill(order_id, info, order):
    sell_price = float(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"]
            )
        )
    )

    STATE["bot_position"] -= info["size"]

    if STATE["bot_position"] < 0:
        STATE["bot_position"] = 0

    buy_price = sell_price - TAKE_PROFIT

    log(
        f"SELL FILLED | "
        f"${price_string(sell_price)} "
        f"| POSITION={STATE['bot_position']} "
        f"| REBUY=${price_string(buy_price)}"
    )

    if (
        STATE["bot_position"] + ORDER_SIZE
        <= MAX_POSITION
    ):
        place_limit(
            "buy",
            buy_price,
            "B"
        )

    info["handled"] = True
    info["fill_price"] = sell_price

    save_state()


# ============================================================
# PROCESS
# ============================================================

def process_orders():
    for order_id, info in list(
        STATE["orders"].items()
    ):

        if info.get("handled"):
            continue

        order = get_order_status(order_id)

        if not order:
            continue

        state = str(
            order.get("state", "")
        ).lower()

        if state in ("open", "pending"):
            continue

        if state == "closed":

            if info.get("side") == "buy":
                handle_buy_fill(
                    order_id,
                    info,
                    order
                )

            elif info.get("side") == "sell":
                handle_sell_fill(
                    order_id,
                    info,
                    order
                )

        elif state in (
            "cancelled",
            "canceled"
        ):
            log(
                f"BOT ORDER CANCELLED | "
                f"ID={order_id}"
            )

            info["handled"] = True
            save_state()


# ============================================================
# MAIN
# ============================================================

def main():
    log("==========================================")
    log("STARTING DIPTI LADDER")
    log("==========================================")

    log("Credentials loaded from AWS Secrets Manager")
    log(f"SYMBOL: {SYMBOL}")
    log(f"PRODUCT ID: {PRODUCT_ID}")
    log(f"BUY STEP: ${BUY_STEP}")
    log(f"TAKE PROFIT: ${TAKE_PROFIT}")
    log(f"ORDER SIZE: {ORDER_SIZE} LOT")
    log(f"MAX POSITION: {MAX_POSITION} LOTS")

    cmp = get_cmp()
    log(f"Delta API OK | CMP=${cmp}")

    startup_reconcile()
    initialize_grid()

    log("DIPTI LADDER RUNNING")

    while True:
        try:
            process_orders()
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("DIPTI LADDER STOPPED")
            break

        except Exception as e:
            log(
                f"MAIN LOOP ERROR | "
                f"{type(e).__name__}: {e}"
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
