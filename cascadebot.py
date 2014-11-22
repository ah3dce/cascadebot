"""
Cascade lending bot for Bitfinex. Places lending offers at a high rate, then
gradually lowers them until they're filled.

This is intended as a proof of concept alternative to fractional reserve rate
(FRR) loans. FRR lending heavily distorts the swap market on Bitfinex. My hope
is that Bitfinex will remove the FRR, and implement an on-site version of this
bot for lazy lenders (myself included) to use instead.

Git repo: https://github.com/ah3dce/cascadebot
Bitcoin tips: 1Fk1G8yVtXQLC1Eft4r1kS8e3SZyRaFwbM

Requires Python 3 and the requests library:

    https://pypi.python.org/pypi/requests/

Fill in the parameters below, and then run it with:

    python3 cascadebot.py

"""
from decimal import Decimal
from datetime import datetime, timedelta

# API key stuff
BITFINEX_API_KEY = "API_KEY_GOES_HERE"
BITFINEX_API_SECRET = b"API_SECRET_GOES_HERE"

# Set this to False if you don't want the bot to make USD offers
LEND_USD = True

# Set this to False if you don't want the bot to make BTC offers
LEND_BTC = True

# Rate to start our USD offers at, in percentage per year
USD_START_RATE_PERCENT = Decimal("365.0")

# Don't reduce our USD offers below this rate, in percentage per year
USD_MINIMUM_RATE_PERCENT = Decimal("YOU HAVE TO PICK THIS ONE YOURSELF")

# How often to reduce the rates on our unfilled USD offers
USD_RATE_DECREMENT_INTERVAL = timedelta(hours=1)

# How much to reduce the rates on our unfilled USD offers, in percentage per
# year
USD_RATE_DECREMENT_PERCENT = Decimal("35.0")

# How many days we're willing to lend our USD funds for
USD_LEND_PERIOD_DAYS = 30

# Don't try to make USD offers smaller than this. Bitfinex currently doesn't
# allow loan offers smaller than $50.
USD_MINIMUM_LEND_AMOUNT = Decimal("50.0")

# Rate to start our BTC offers at, in percentage per year
BTC_START_RATE_PERCENT = Decimal("20.0")

# Don't reduce our BTC offers below this rate, in percentage per year
BTC_MINIMUM_RATE_PERCENT = Decimal("YOU HAVE TO PICK THIS ONE YOURSELF")

# How often to reduce the rates on our unfilled BTC offers
BTC_RATE_DECREMENT_INTERVAL = timedelta(hours=1)

# How much to reduce the rates on our unfilled BTC offers, in percentage per
# year
BTC_RATE_DECREMENT_PERCENT = Decimal("2.0")

# How many days we're willing to lend our BTC funds for
BTC_LEND_PERIOD_DAYS = 30

# Don't try to make BTC offers smaller than this. Bitfinex currently doesn't
# allow loan offers smaller than $50.
BTC_MINIMUM_LEND_AMOUNT = Decimal("0.25")

# How often to retrieve the current statuses of our offers
POLL_INTERVAL = timedelta(minutes=5)


from itertools import count
import time
import base64
import json
import hmac
import hashlib
from collections import defaultdict, deque

import requests


class Offer(object):
    """
    An unfilled swap offer.

    """
    def __init__(self, offer_dict):
        """
        Args:
            offer_dict: Dictionary of data for a single swap offer as returned
                by the Bitfinex API.

        """
        self.id = offer_dict["id"]
        self.currency = offer_dict["currency"]
        self.rate = Decimal(offer_dict["rate"])
        self.submitted_at = datetime.utcfromtimestamp(int(Decimal(
            offer_dict["timestamp"]
        )))
        self.amount = Decimal(offer_dict["remaining_amount"])

    def __repr__(self):
        return (
            "Offer(id={}, currency='{}', rate={}, amount={}, submitted_at={})"
        ).format(self.id, self.currency, self.rate, self.amount,
                 self.submitted_at)

    def get_new_rate(self):
        """
        Calculate what the interest rate on this offer should be changed to,
        based on how much time has elapsed since it was submitted.

        Returns:
            The new interest rate as a Decimal object, or None if the rate
            should not be changed.

        """
        min_rate, rate_decrement, decrement_interval = None, None, None
        if self.currency == "USD":
            min_rate = USD_MINIMUM_RATE_PERCENT
            rate_decrement = USD_RATE_DECREMENT_PERCENT
            decrement_interval = USD_RATE_DECREMENT_INTERVAL
        elif self.currency == "BTC":
            min_rate = BTC_MINIMUM_RATE_PERCENT
            rate_decrement = BTC_RATE_DECREMENT_PERCENT
            decrement_interval = BTC_RATE_DECREMENT_INTERVAL
        else:
            raise Exception("Unrecognized currency string")

        if self.rate <= min_rate:
            return None
        time_elapsed = datetime.utcnow() - self.submitted_at
        intervals_elapsed = time_elapsed // decrement_interval
        if intervals_elapsed < 1:
            return None
        total_reduction = rate_decrement * intervals_elapsed
        new_rate = self.rate - total_reduction
        return max(new_rate, min_rate)


class BitfinexAPI(object):
    """
    Handles API requests and responses.

    """
    base_url = "https://api.bitfinex.com"
    rate_limit_interval = timedelta(seconds=70)
    max_requests_per_interval = 60

    def __init__(self, api_key, api_secret):
        """
        Args:
            api_key: The API key to use for requests made by this object.
            api_secret: THe API secret to use for requests made by this object.

        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.nonce = count(int(time.time()))
        self.request_timestamps = deque()

    def get_offers(self):
        """
        Retrieve current offers.

        Returns:
            A 2-tuple of lists. The first contains USD offers and the second
            contains BTC offers, as Offer objects.

        """
        offers_data = self._request("/v1/offers")
        usd_offers = []
        btc_offers = []
        for offer_dict in offers_data:
            # Ignore swap demands and FRR offers
            if (
                offer_dict["direction"] == "lend"
                and offer_dict["rate"] != "0.0"
            ):
                offer = Offer(offer_dict)
                if offer.currency == "USD":
                    usd_offers.append(offer)
                elif offer.currency == "BTC":
                    btc_offers.append(offer)
        return (usd_offers, btc_offers)

    def cancel_offer(self, offer):
        """
        Cancel an offer.

        Args:
            offer: The offer to cancel as an Offer object.

        Returns:
            An Offer object representing the now-cancelled offer.

        """
        return Offer(self._request("/v1/offer/cancel", {"offer_id": offer.id}))

    def new_offer(self, currency, amount, rate, period):
        """
        Create a new offer.

        Args:
            currency: Either "USD" or "BTC".
            amount: Amount of the offer as a Decimal object.
            rate: Interest rate of the offer per year, as a Decimal object.
            period: How many days to lend for.

        Returns:
            An Offer object representing the newly-created offer.

        """
        return Offer(self._request("/v1/offer/new", {
            "currency": currency,
            "amount": str(amount),
            "rate": str(rate),
            "period": period,
            "direction": "lend",
        }))

    def get_available_balances(self):
        """
        Retrieve available balances in deposit wallet.

        Returns:
            A 2-tuple of the USD balance followed by the BTC balance.

        """
        balances_data = self._request("/v1/balances")
        usd_available = 0
        btc_available = 0
        for balance_data in balances_data:
            if balance_data["type"] == "deposit":
                if balance_data["currency"] == "usd":
                    usd_available = Decimal(balance_data["available"])
                elif balance_data["currency"] == "btc":
                    btc_available = Decimal(balance_data["available"])
        return (usd_available, btc_available)

    def _request(self, request_type, parameters=None):
        self._rate_limiter()
        url = self.base_url + request_type
        if parameters is None:
            parameters = {}
        parameters.update({"request": request_type,
                           "nonce": str(next(self.nonce))})
        payload = base64.b64encode(json.dumps(parameters).encode())
        signature = hmac.new(self.api_secret, payload, hashlib.sha384)
        headers = {"X-BFX-APIKEY": self.api_key,
                   "X-BFX-PAYLOAD": payload,
                   "X-BFX-SIGNATURE": signature.hexdigest()}
        request = None
        retry_count = 0
        while request is None:
            try:
                request = requests.post(url, headers=headers)
            except requests.exceptions.ConnectionError:
                delay = 2 ** retry_count
                print("Connection failed, sleeping for", delay,
                      "seconds before retrying")
                time.sleep(delay)
                retry_count += 1
                # I'm assuming that if we don't manage to connect, it doesn't
                # count against our request limit. If this isn't the case, then
                # we should call _rate_limiter() here too.
        if request.status_code != 200:
            print(request.text)
            request.raise_for_status()
        return request.json()

    def _rate_limiter(self):
        timestamps = self.request_timestamps
        while True:
            expire = datetime.utcnow() - self.rate_limit_interval
            while timestamps and timestamps[0] < expire:
                timestamps.popleft()
            if len(timestamps) >= self.max_requests_per_interval:
                delay = (timestamps[0] - expire).total_seconds()
                print("Request rate limit hit, sleeping for", delay, "seconds")
                time.sleep(delay)
            else:
                break
        timestamps.append(datetime.utcnow())


def adjust_offers(api, offers, lend_period, minimum_amount):
    """
    Check the specified offers and adjust them as needed.

    Args:
        api: Instance of BitfinexAPI to use.
        offers: Current offers to be adjusted.
        lend_period: How long we're willing to lend our funds for.
        minimum_amount: Make sure any new offers are this amount or higher.

    """
    new_offer_amounts = defaultdict(Decimal)
    if not offers:
        return
    currency = offers[0].currency
    for offer in offers:
        new_rate = offer.get_new_rate()
        if new_rate is not None:
            cancelled_offer = api.cancel_offer(offer)
            new_offer_amounts[new_rate] += cancelled_offer.amount
    for rate, amount in new_offer_amounts.items():
        # The minimum loan amount can cause some weirdness here. If one of our
        # offers gets partially filled and the remainder is below the minimum,
        # we won't be able to place it at the new rate after cancelling. It'll
        # end up with the rest of our funds which get lent out at our starting
        # (highest) rate. The alternative would be to leave small partially
        # filled offers alone, which would mean they no longer get moved down.
        if amount > minimum_amount:
            print(api.new_offer(currency, amount, rate, lend_period))
        else:
            print("At rate {}, {} offer amount {} is below minimum,"
                  " skipping".format(rate, currency, amount))


def go():
    """
    Main loop.

    """
    api = BitfinexAPI(BITFINEX_API_KEY, BITFINEX_API_SECRET)
    print("Ctrl+C to quit")
    while True:
        start_time = datetime.utcnow()

        usd_offers, btc_offers = api.get_offers()
        print(usd_offers)
        print(btc_offers)
        if LEND_USD:
            adjust_offers(api, usd_offers, USD_LEND_PERIOD_DAYS,
                          USD_MINIMUM_LEND_AMOUNT)
        if LEND_BTC:
            adjust_offers(api, btc_offers, BTC_LEND_PERIOD_DAYS,
                          BTC_MINIMUM_LEND_AMOUNT)

        usd_available, btc_available = api.get_available_balances()
        if LEND_USD and usd_available >= USD_MINIMUM_LEND_AMOUNT:
            print(api.new_offer("USD", usd_available, USD_START_RATE_PERCENT,
                                USD_LEND_PERIOD_DAYS))
        if LEND_BTC and btc_available >= BTC_MINIMUM_LEND_AMOUNT:
            print(api.new_offer("BTC", btc_available, BTC_START_RATE_PERCENT,
                                BTC_LEND_PERIOD_DAYS))

        end_time = datetime.utcnow()
        elapsed = end_time - start_time
        remaining = POLL_INTERVAL - elapsed
        delay = max(remaining.total_seconds(), 0)
        print("Done processing, sleeping for", delay, "seconds")
        time.sleep(delay)


go()


# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
#
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org/>
