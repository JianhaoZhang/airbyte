#
# MIT License
#
# Copyright (c) 2020 Airbyte
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
import json
from time import sleep
from typing import List, Mapping, Any

import backoff
import pendulum
from airbyte_cdk.entrypoint import logger
from cached_property import cached_property
from facebook_business import FacebookAdsApi
from facebook_business.adobjects import user as fb_user
from facebook_business.adobjects.iguser import IGUser
from facebook_business.adobjects.page import Page
from facebook_business.exceptions import FacebookRequestError

from source_instagram.common import InstagramAPIException, retry_pattern

backoff_policy = retry_pattern(backoff.expo, FacebookRequestError, max_tries=7, factor=5)


class MyFacebookAdsApi(FacebookAdsApi):
    """Custom Facebook API class to intercept all API calls and handle call rate limits"""

    call_rate_threshold = 90  # maximum percentage of call limit utilization
    pause_interval = pendulum.duration(minutes=1)  # default pause interval if reached or close to call rate limit

    @staticmethod
    def parse_call_rate_header(headers):
        call_count = 0
        pause_interval = pendulum.duration()

        usage_header = headers.get("x-business-use-case-usage") or headers.get("x-app-usage") or headers.get("x-ad-account-usage")
        if usage_header:
            usage_header = json.loads(usage_header)
            call_count = usage_header.get("call_count") or usage_header.get("acc_id_util_pct") or 0
            pause_interval = pendulum.duration(minutes=usage_header.get("estimated_time_to_regain_access", 0))

        return call_count, pause_interval

    def handle_call_rate_limit(self, response, params):
        headers = response.headers()
        call_count, pause_interval = self.parse_call_rate_header(headers)
        if call_count > self.call_rate_threshold or pause_interval:
            logger.warn(f"Utilization is too high ({call_count})%, pausing for {pause_interval}")
            sleep(pause_interval.total_seconds())

    @backoff_policy
    def call(
        self,
        method,
        path,
        params=None,
        headers=None,
        files=None,
        url_override=None,
        api_version=None,
    ):
        """Makes an API call, delegate actual work to parent class and handles call rates"""
        response = super().call(method, path, params, headers, files, url_override, api_version)
        self.handle_call_rate_limit(response, params)
        return response


class InstagramAPI:
    def __init__(self, access_token: str):
        self._api = FacebookAdsApi.init(access_token=access_token)
        # design flaw in MyFacebookAdsApi requires such strange set of new default api instance
        self.api = MyFacebookAdsApi.init(access_token=access_token, crash_log=False)
        FacebookAdsApi.set_default_api(self.api)

    @cached_property
    def accounts(self) -> List[Mapping[str, Any]]:
        return self._find_accounts()

    def _find_accounts(self) -> List[Mapping[str, Any]]:
        try:
            instagram_business_accounts = []
            accounts = fb_user.User(fbid="me").get_accounts()
            for account in accounts:
                page = Page(account.get_id()).api_get(fields=["instagram_business_account"])
                if page.get("instagram_business_account"):
                    instagram_business_accounts.append(
                        {
                            "page_id": account.get_id(),
                            "instagram_business_account": IGUser(page.get("instagram_business_account").get("id")),
                        }
                    )
        except FacebookRequestError as exc:
            raise InstagramAPIException(f"Error: {exc.api_error_code()}, {exc.api_error_message()}") from exc

        if not instagram_business_accounts:
            raise InstagramAPIException("Couldn't find an Instagram business account for current Access Token")

        return instagram_business_accounts
