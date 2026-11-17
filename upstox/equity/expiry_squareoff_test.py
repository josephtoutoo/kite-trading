#!/usr/bin/env python3
"""Unit tests for expiry-week square-off date and cancel-policy helpers."""
import datetime
import sys
import unittest

sys.path.insert(0, '/workspace/upstox/equity')
sys.path.insert(0, '/workspace/upstox')
import expiry_squareoff as es

TODAY = datetime.date(2026, 11, 16)  # Monday, ISO week 47


class ExpiryWeekTests(unittest.TestCase):
    def test_today_is_not_expiry_week_for_23_24_26_nov(self):
        for exp in (datetime.date(2026, 11, 23),
                    datetime.date(2026, 11, 24),
                    datetime.date(2026, 11, 26),
                    '2026-11-23', '2026-11-24', '2026-11-26'):
            self.assertFalse(es.in_expiry_week(exp, TODAY), exp)

    def test_old_seven_day_window_would_have_fired_on_23_nov(self):
        exp = datetime.date(2026, 11, 23)
        # The pre-fix rolling window: this is why today's close was a bug.
        self.assertEqual((exp - TODAY).days, 7)
        self.assertTrue(0 <= (exp - TODAY).days <= 7)
        self.assertFalse(es.in_expiry_week(exp, TODAY))

    def test_fires_only_in_the_contract_week(self):
        exp = datetime.date(2026, 11, 23)  # Monday, ISO week 48
        self.assertFalse(es.in_expiry_week(exp, datetime.date(2026, 11, 20)))  # Fri before
        self.assertTrue(es.in_expiry_week(exp, datetime.date(2026, 11, 23)))
        self.assertFalse(es.in_expiry_week(exp, datetime.date(2026, 11, 24)))  # already expired
        # 24 / 26 Nov contracts are in week 48 from Mon 23 Nov
        self.assertTrue(es.in_expiry_week(datetime.date(2026, 11, 24), datetime.date(2026, 11, 23)))
        self.assertTrue(es.in_expiry_week(datetime.date(2026, 11, 26), datetime.date(2026, 11, 23)))
        self.assertTrue(es.in_expiry_week(datetime.date(2026, 11, 26), datetime.date(2026, 11, 26)))

    def test_missing_or_past_expiry(self):
        self.assertFalse(es.in_expiry_week(None, TODAY))
        self.assertFalse(es.in_expiry_week('', TODAY))
        self.assertFalse(es.in_expiry_week(datetime.date(2026, 11, 15), TODAY))


class CancelPolicyTests(unittest.TestCase):
    def test_does_not_cancel_at_or_above_market_unless_needed(self):
        # today's cancelled user limits: 21 vs 16.15, 52 vs 48.75 — far above market
        self.assertFalse(es.user_limit_sell_cancel_needed(21, 16.15, flattening=False))
        self.assertFalse(es.user_limit_sell_cancel_needed(52, 48.75, flattening=False))
        # at market, or slightly above, even when flattening
        self.assertFalse(es.user_limit_sell_cancel_needed(16.15, 16.15, flattening=True))
        self.assertFalse(es.user_limit_sell_cancel_needed(16.20, 16.15, flattening=True))
        self.assertFalse(es.user_limit_sell_cancel_needed(16.00, 16.15, flattening=True))
        # far above, only when a genuine flatten needs the qty
        self.assertTrue(es.user_limit_sell_cancel_needed(21, 16.15, flattening=True))
        self.assertTrue(es.user_limit_sell_cancel_needed(52, 48.75, flattening=True))

    def test_plan_leaves_above_market_user_limits_when_not_needed(self):
        orders = [{
            'trading_symbol': 'INDHOTEL 880 CE 23 NOV 26', 'transaction_type': 'SELL',
            'status': 'open', 'order_type': 'LIMIT', 'price': 21, 'quantity': 2000,
            'filled_quantity': 0, 'order_id': 'u1',
        }]
        plan = es.plan_open_sells(orders, 'INDHOTEL26NOV880CE', 2000, 16.15, flattening=False)
        self.assertEqual(plan['cancel'], [])
        self.assertEqual(len(plan['leave']), 1)
        self.assertEqual(plan['sell_qty'], 0)

    def test_plan_cancels_far_above_market_limit_only_when_flattening(self):
        orders = [{
            'tradingsymbol': 'PAYTM26NOV1880CE', 'transaction_type': 'SELL',
            'status': 'open', 'order_type': 'LIMIT', 'price': 52, 'quantity': 725,
            'filled_quantity': 0, 'order_id': 'k1',
        }]
        plan = es.plan_open_sells(orders, 'PAYTM26NOV1880CE', 1450, 48.75, flattening=True)
        self.assertEqual(len(plan['cancel']), 1)
        self.assertEqual(plan['cancel'][0]['order_id'], 'k1')
        self.assertEqual(plan['sell_qty'], 1450)

    def test_plan_recognises_sl_m(self):
        orders = [{
            'tradingsymbol': 'PAYTM26NOV1880CE', 'transaction_type': 'SELL',
            'status': 'trigger pending', 'order_type': 'SL-M', 'price': 36.40,
            'trigger_price': 36.40, 'quantity': 1450, 'filled_quantity': 0, 'order_id': 'sl1',
        }]
        plan = es.plan_open_sells(orders, 'PAYTM26NOV1880CE', 1450, 48.75, flattening=True)
        self.assertEqual(len(plan['cancel']), 1)
        self.assertEqual(plan['cancel'][0]['order_id'], 'sl1')


class MasterExpiryTests(unittest.TestCase):
    def test_instrument_master_expiry_is_23_nov(self):
        for sym in ('INDHOTEL26NOV880CE', 'PAYTM26NOV1880CE',
                    'INDHOTEL 880 CE 23 NOV 26', 'PAYTM 1880 CE 23 NOV 26'):
            exp = es.expiry_for(sym)
            self.assertEqual(exp, datetime.date(2026, 11, 23), (sym, exp))
            self.assertFalse(es.in_expiry_week(exp, TODAY), sym)


    def test_next_month_spread_is_not_squared_in_nov_expiry_week(self):
        # Nov stock monthly is Mon 23 Nov (ISO week 48). Next month is 29 Dec.
        # The expiry-week routine running that week must leave the December legs alone.
        nov = datetime.date(2026, 11, 23)
        dec = datetime.date(2026, 12, 29)
        self.assertTrue(es.in_expiry_week(nov, nov))
        self.assertFalse(es.in_expiry_week(dec, nov))
        skip, why = es.skip_next_month_spread(dec, nov)
        self.assertTrue(skip)
        skip_now, _ = es.skip_next_month_spread(nov, nov)
        self.assertFalse(skip_now)
        ledger = {'positions': [{'long': 'RELIANCE26DEC1260CE', 'short': 'RELIANCE26DEC1300CE'}]}
        # A December contract is skipped in the November expiry week (later expiry).
        skip, why = es.skip_next_month_spread(dec, nov, 'RELIANCE26DEC1260CE', ledger)
        self.assertTrue(skip)
        # In the spread's own expiry week the pair is closed (short first), not skipped.
        skip_own, _ = es.skip_next_month_spread(dec, dec, 'RELIANCE26DEC1260CE', ledger)
        self.assertFalse(skip_own)


if __name__ == '__main__':
    unittest.main()
