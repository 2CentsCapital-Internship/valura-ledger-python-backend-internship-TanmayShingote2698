"""Your ledger. This is the whole assignment.

`client.py` handles the network and hands you one event at a time. You return
the journal legs it produced. Some events correctly produce none: return an
empty list, not None-as-an-accident.

One event type is implemented as a worked example. The rest raise, with the rule
from PROTOCOL.md quoted in the message, so a practice run tells you exactly what
is left rather than silently scoring zero.

Two things to get right before anything else:

  * Use `Decimal`, never `float`. Money here does not always divide evenly, and
    a float implementation will disagree with us by a cent in places you will
    struggle to find.
  * Key balances by (customer, account), not by account. At least one event
    moves money between two customers on the same account, and an
    account-level book shows nothing wrong at all.
"""
from __future__ import annotations
from decimal import InvalidOperation
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")


def money(x: Decimal) -> Decimal:
    """2 decimal places, half away from zero. Not round(), which is half-even."""
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(D(debit))), "credit": str(money(D(credit)))}


class Book:
    def __init__(self) -> None:
        # balances[(customer_id, account)] = debit-positive balance
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()

        # -------- Runtime State --------

        # withdrawal_id -> {"customer_id":..., "amount":...}
        self.withdrawals = {}

        # fee_event_id -> amount
        self.fee_events = {}

        # trade_id -> trade details
        self.trades = {}

        # order_id -> order details
        self.orders = {}

        # Cash hold per customer
        self.cash_holds = defaultdict(lambda: ZERO)

        # Shares on hold (customer -> symbol -> quantity)
        self.share_holds = defaultdict(lambda: defaultdict(lambda: D("0")))

        # Open order routing
        self.open_order_routes = {}

        # customer -> symbol -> FIFO lots
        self.positions = defaultdict(lambda: defaultdict(list))

        # event_id -> original journal legs
        self.original_postings = {}

        # todo list shown after run
        self.todo: dict[str, int] = defaultdict(int)

    # -----------------------------------------------------------------------
    BROKERS = {
    "BRK-A": {
        "asset_classes": {"equity", "etf"},
        "brokerage": D("0.0020"),
        "custody": D("0.0004"),
    },
    "BRK-B": {
        "asset_classes": {"equity", "bond"},
        "brokerage": D("0.0015"),
        "custody": D("0.0005"),
    },
    "BRK-C": {
        "asset_classes": {"etf", "bond"},
        "brokerage": D("0.0025"),
        "custody": D("0.0003"),
    },
}

    def choose_broker(self, asset_class, principal):
        best = None

        for broker, cfg in sorted(self.BROKERS.items()):
            if asset_class not in cfg["asset_classes"]:
                continue

            charge = money(
                principal * cfg["brokerage"] +
                principal * cfg["custody"]
            )

            if best is None or charge < best[0]:
                best = (charge, broker)

        return best[1]
    def apply(self, ev: dict) -> list[dict]:
        """Post one event and return its legs.

        The same event_id can arrive more than once, and the server will
        deliberately re-send several hundred events partway through the run.
        Posting twice is the single most expensive mistake available here.
        """
        eid = ev["event_id"]
        if eid in self.seen:
            return []                      # already posted; nothing new happens
        self.seen.add(eid)

        handler = getattr(self, "on_" + ev["type"], None)
        if handler is None:
            self.todo[ev["type"]] += 1
            return []
        try:
            legs = handler(ev["payload"], ev) or []
        except NotImplementedError:
            # Not written yet. Submit nothing for it and carry on, so one
            # missing handler costs you that event rather than the whole run.
            self.todo[ev["type"]] += 1
            return []
        except Rejected:
            # An event you refuse still gets a submission, with no legs, and it
            # must leave your book exactly as it was.
            return []
        self._post(legs)

        if legs:
            self.original_postings[eid] = [dict(l) for l in legs]

        return legs

    def _post(self, legs):

        if not legs:
            return

        dr = sum((D(l["debit"]) for l in legs), ZERO)
        cr = sum((D(l["credit"]) for l in legs), ZERO)

        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")

        for l in legs:
            self.balances[(l["customer_id"], l["account"])] += (
                D(l["debit"]) - D(l["credit"])
            )
            

    # -- worked example -----------------------------------------------------
    def on_deposit(self, p: dict, ev: dict) -> list[dict]:
        """Cash arrives, and the firm owes the customer more.

            Dr 1100 amount        Cr 2010 amount
        """
        amount = money(D(p["amount"]))
        cid = p["customer_id"]
        return [leg("1100", cid, debit=amount),
                leg("2010", cid, credit=amount)]

    # -- yours --------------------------------------------------------------
    def on_fee_charged(self, p, ev):
         cid = p["customer_id"]

         try:
            amount = money(D(p["amount"]))
         except (InvalidOperation, KeyError, TypeError):
            raise Rejected()

         self.fee_events[ev["event_id"]] = {
            "customer_id": cid,
            "amount": amount,
            "refunded": False,
        }

         return [
            leg("2010", cid, debit=amount),
            leg("1100", cid, credit=amount),
        ]

    def on_fee_refund(self, p, ev):
        src = p.get("refunds_source_id")

        fee = self.fee_events.get(src)

        if fee is None:
            raise Rejected()

        if fee["refunded"]:
            raise Rejected()

        fee["refunded"] = True

        return [
            leg("1100", fee["customer_id"], debit=fee["amount"]),
            leg("2010", fee["customer_id"], credit=fee["amount"])
        ]

    def on_interest_credited(self, p, ev):
        try:
            cid = p["customer_id"]
            gross = money(D(p["gross_amount"]))
            share = money(D(p["customer_share"]))
        except (KeyError, InvalidOperation, TypeError):
            raise Rejected()

        income = money(gross - share)

        return [
            leg("1100", cid, debit=gross),
            leg("2010", cid, credit=share),
            leg("4200", cid, credit=income)
        ]

    def on_transfer_between_customers(self, p, ev):
        try:
            amount = money(D(p["amount"]))
        except (KeyError, InvalidOperation, TypeError):
            raise Rejected()

        return [
            leg("2010", p["from_customer_id"], debit=amount),
            leg("2010", p["to_customer_id"], credit=amount)
        ]

    def on_fx_deposit(self, p, ev):
        try:
            cid = p["customer_id"]

            market = money(D(p["usd_at_market_rate"]))
            customer = money(D(p["usd_at_customer_rate"]))
        except (KeyError, InvalidOperation, TypeError):
            raise Rejected()

        if customer > market:
            raise Rejected()

        spread = money(market - customer)

        return [
            leg("1100", cid, debit=market),
            leg("2010", cid, credit=customer),
            leg("4100", cid, credit=spread)
        ]

    def on_withdrawal_requested(self, p, ev):
            try:
                amount = money(D(p["amount"]))
                cid = p["customer_id"]
                wid = p["withdrawal_id"]
            except (KeyError, InvalidOperation, TypeError):
                raise Rejected()

            self.withdrawals[wid] = {
            "customer_id": cid,
            "amount": amount,
            "status": "pending"
        }

            return [
            leg("2010", cid, debit=amount),
            leg("2300", cid, credit=amount)
        ]
            

    def on_withdrawal_settled(self, p, ev):
        wid = p.get("withdrawal_id")

        if wid not in self.withdrawals:
            raise Rejected()

        wd = self.withdrawals[wid]

        if wd["status"] != "pending":
            raise Rejected()

        wd["status"] = "settled"

        return [
            leg("2300", wd["customer_id"], debit=wd["amount"]),
            leg("1100", wd["customer_id"], credit=wd["amount"])
        ]

    def on_withdrawal_rejected(self, p, ev):
        wid = p.get("withdrawal_id")

        if wid not in self.withdrawals:
            raise Rejected()

        wd = self.withdrawals[wid]

        if wd["status"] != "pending":
            raise Rejected()

        wd["status"] = "rejected"

        return [
            leg("2300", wd["customer_id"], debit=wd["amount"]),
            leg("2010", wd["customer_id"], credit=wd["amount"])
        ]
    def on_order_placed(self, p, ev):
        try:
            oid = p["order_id"]
            cid = p["customer_id"]
            side = p["side"]

            qty = D(p["quantity"])
            limit_price = D(p["limit_price"])
            est = money(D(p["est_charges"]))

        except Exception:
            raise Rejected()

        principal = money(qty * limit_price)

        broker = self.choose_broker(
            p["asset_class"],
            principal
        )

        self.orders[oid] = dict(p)

        self.open_order_routes[oid] = broker

        if side == "buy":
            self.cash_holds[cid] += principal + est
        else:
            self.share_holds[cid][p["symbol"]] += qty

        return []

    def on_order_partially_filled(self, p, ev):
        return self.on_order_filled(p, ev)

    def on_order_filled(self, p, ev):
        def on_order_filled(self, p, ev):
            print("\n===== ORDER FILLED PAYLOAD =====")
            print(p)
            raise NotImplementedError()

    def on_trade_settled(self, p, ev):
        raise NotImplementedError(
            "buy: Dr 2350 / Cr 1100.  sell: Dr 1100 / Cr 1150")

    def on_order_cancelled(self, p, ev):
        oid = p["order_id"]

        order = self.orders.pop(oid, None)

        if order is None:
            raise Rejected()

        cid = order["customer_id"]

        qty = D(order["quantity"])

        if order["side"] == "buy":
            principal = money(qty * D(order["limit_price"]))
            est = money(D(order["est_charges"]))
            self.cash_holds[cid] -= principal + est
        else:
            self.share_holds[cid][order["symbol"]] -= qty

        self.open_order_routes.pop(oid, None)

        return []

    def on_order_rejected(self, p, ev):
        return self.on_order_cancelled(p, ev)

    def on_dividend_cash(self, p, ev):
        raise NotImplementedError(
            "Dr 1100 net / Cr 2010 net. Tax is withheld at source, so raise no "
            "payable")

    def on_dividend_reinvested(self, p, ev):
        raise NotImplementedError(
            "Dr 1200 net / Cr 2100 net, and add a lot. Cash is not involved")

    def on_stock_split(self, p, ev):
        raise NotImplementedError(
            "No legs. Quantity scales; total cost does not change")

    def on_symbol_change(self, p, ev):
        raise NotImplementedError("No legs. Re-key the holding")

    def on_reversal(self, p, ev):
        raise NotImplementedError(
            "Post the exact inverse of the original's legs, and undo its effect "
            "on your LOT BOOK too. A reversed buy whose lot you leave behind "
            "balances perfectly and corrupts every later cost basis")

    # -- reporting ----------------------------------------------------------
    def snapshot(self) -> dict:
        """What a checkpoint_request wants: your whole state, right now.

        Report every account you have ever posted to, including any that have
        netted back to zero. Trial balance values are debit-positive, so
        liabilities carry a negative sign.
        """
        tb: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (_cid, acct), bal in self.balances.items():
            tb[acct] += bal

        customers: dict[str, dict] = {}
        for (cid, acct), bal in self.balances.items():
            c = customers.setdefault(cid, {"wallet_cash": ZERO,
                                           "cash_hold": ZERO, "positions": {}})
            if acct == "2010":
                c["wallet_cash"] += -bal          # a liability, so credit-positive

        return {
            "trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
            "customers": {cid: {"wallet_cash": str(money(c["wallet_cash"])),
                                "cash_hold": str(money(c["cash_hold"])),
                                "positions": c["positions"]}
                          for cid, c in sorted(customers.items())},
        }


class Rejected(Exception):
    """Raise from a handler for an event you refuse to post.

    An oversell, a reversal of something you never received, a payload that
    will not parse. Rejecting one event and carrying on beats stopping: a
    server that stalls misses everything after it.
    """

 