import asyncio
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.bitmart import (
    bitmart_constants as CONSTANTS,
    bitmart_utils,
    bitmart_web_utils as web_utils,
)
from hummingbot.connector.exchange.bitmart.bitmart_api_order_book_data_source import BitmartAPIOrderBookDataSource
from hummingbot.connector.exchange.bitmart.bitmart_api_user_stream_data_source import BitmartAPIUserStreamDataSource
from hummingbot.connector.exchange.bitmart.bitmart_auth import BitmartAuth
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class BitmartExchange(ExchangePyBase):
    """
    BitmartExchange connects with BitMart exchange and provides order book pricing, user account tracking and
    trading functionality.
    """
    API_CALL_TIMEOUT = 10.0
    POLL_INTERVAL = 1.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    UPDATE_TRADE_STATUS_MIN_INTERVAL = 10.0

    web_utils = web_utils

    def __init__(self,
                 client_config_map: "ClientConfigAdapter",
                 bitmart_api_key: str,
                 bitmart_secret_key: str,
                 bitmart_memo: str,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 ):
        """
        :param bitmart_api_key: The API key to connect to private BitMart APIs.
        :param bitmart_secret_key: The API secret.
        :param trading_pairs: The market trading pairs which to track order book data.
        :param trading_required: Whether actual trading is needed.
        """
        self._api_key: str = bitmart_api_key
        self._secret_key: str = bitmart_secret_key
        self._memo: str = bitmart_memo
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs

        super().__init__(client_config_map)
        self.real_time_balance_update = False

    @property
    def authenticator(self):
        return BitmartAuth(
            api_key=self._api_key,
            secret_key=self._secret_key,
            memo=self._memo,
            time_provider=self._time_synchronizer)

    @property
    def name(self) -> str:
        return "bitmart"

    @property
    def rate_limits_rules(self):
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self):
        return CONSTANTS.DEFAULT_DOMAIN

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.GET_TRADING_RULES_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.GET_TRADING_RULES_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.CHECK_NETWORK_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector.
        Note that Market order type is no longer required and will not be used.
        """
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER]

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception)
        is_time_synchronizer_related = ("Header X-BM-TIMESTAMP" in error_description
                                        and ("30007" in error_description or "30008" in error_description))
        return is_time_synchronizer_related

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        # TODO: implement this method correctly for the connector
        # The default implementation was added when the functionality to detect not found orders was introduced in the
        # ExchangePyBase class. Also fix the unit test test_lost_order_removed_if_not_found_during_order_status_update
        # when replacing the dummy implementation
        return False

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        # TODO: implement this method correctly for the connector
        # The default implementation was added when the functionality to detect not found orders was introduced in the
        # ExchangePyBase class. Also fix the unit test test_cancel_order_not_found_in_the_exchange when replacing the
        # dummy implementation
        return False

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return BitmartAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return BitmartAPIUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> AddedToCostTradeFee:
        """
        To get trading fee, this function is simplified by using fee override configuration. Most parameters to this
        function are ignore except order_type. Use OrderType.LIMIT_MAKER to specify you want trading fee for
        maker order.
        """
        is_maker = order_type is OrderType.LIMIT_MAKER
        return AddedToCostTradeFee(percent=self.estimate_fee_pct(is_maker))

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:

        api_params = {"symbol": await self.exchange_symbol_associated_to_pair(trading_pair),
                      "side": trade_type.name.lower(),
                      "type": "limit",
                      "size": f"{amount:f}",
                      "price": f"{price:f}",
                      "client_order_id": order_id,
                      }
        order_result = await self._api_post(
            path_url=CONSTANTS.CREATE_ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True)
        exchange_order_id = str(order_result["data"]["order_id"])

        return exchange_order_id, self.current_timestamp

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder) -> bool:
        """
        Sends a cancellation request to the exchange and returns whether the cancellation was successful.
        """
        try:
            api_params = {
                "symbol": await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair),
                "client_order_id": order_id,
            }
            cancel_result = await self._api_post(
                path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
                data=api_params,
                is_auth_required=True,
            )
            return cancel_result.get("data", {}).get("result", False)
        except OSError as e:
            error_message = str(e)
            if "50031" in error_message: 
                self.logger().info(f"Order {order_id} is already completed. Marking as completed.")
                self._order_tracker.process_order_update(
                    OrderUpdate(
                        client_order_id=order_id,
                        exchange_order_id=tracked_order.exchange_order_id,
                        trading_pair=tracked_order.trading_pair,
                        update_timestamp=self.current_timestamp,
                        new_state=OrderState.FILLED,
                    )
                )
                return True
            raise

    async def _format_trading_rules(self, symbols_details: Dict[str, Any]) -> List[TradingRule]:
        """
        Converts json API response into a dictionary of trading rules.
        :param symbols_details: The json API response
        :return A dictionary of trading rules.
        Response Example:
        {
            "code": 1000,
            "trace":"886fb6ae-456b-4654-b4e0-d681ac05cea1",
            "message": "OK",
            "data": {
                "symbols": [
                    {
                        "symbol":"GXC_BTC",
                         "symbol_id":1024,
                         "base_currency":"GXC",
                         "quote_currency":"BTC",
                         "quote_increment":"1.00000000",
                         "base_min_size":"1.00000000",
                         "price_min_precision":6,
                         "price_max_precision":8,
                         "expiration":"NA",
                         "min_buy_amount":"0.00010000",
                         "min_sell_amount":"0.00010000"
                    },
                    ...
                ]
            }
        }
        """
        result = []
        for rule in symbols_details["data"]["symbols"]:
            if bitmart_utils.is_exchange_information_valid(rule):
                try:
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(rule["symbol"])
                    price_decimals = Decimal(str(rule["price_max_precision"]))
                    # E.g. a price decimal of 2 means 0.01 incremental.
                    price_step = Decimal("1") / Decimal(str(math.pow(10, price_decimals)))
                    result.append(TradingRule(trading_pair=trading_pair,
                                              min_order_size=Decimal(str(rule["base_min_size"])),
                                              min_order_value=Decimal(str(rule["min_buy_amount"])),
                                              min_base_amount_increment=Decimal(str(rule["base_min_size"])),
                                              min_price_increment=price_step))
                except Exception:
                    self.logger().exception(f"Error parsing the trading pair rule {rule}. Skipping.")
        return result

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _update_balances(self):
        """
        Calls REST API to update total and available balances.
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()
        account_info = await self._api_get(
            path_url=CONSTANTS.GET_ACCOUNT_SUMMARY_PATH_URL,
            is_auth_required=True)
        for account in account_info["data"]["wallet"]:
            asset_name = account["id"]
            self._account_available_balances[asset_name] = Decimal(str(account["available"]))
            self._account_balances[asset_name] = Decimal(str(account["available"])) + Decimal(str(account["frozen"]))
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    async def _request_order_update(self, order: InFlightOrder) -> Dict[str, Any]:
        try:
            response = await self._api_post(
                path_url=CONSTANTS.GET_ORDER_DETAIL_PATH_URL,
                data={"orderId": order.exchange_order_id},
                is_auth_required=True
            )
            if not response or "data" not in response:
                self.logger().error(f"Invalid response for order {order.client_order_id}: {response}")
                return {}
            return response
        except Exception as e:
            self.logger().error(f"Error fetching order update for {order.client_order_id}: {e}")
            return {}

    async def _request_order_fills(self, order: InFlightOrder) -> Dict[str, Any]:
        return await self._api_post(
            path_url=CONSTANTS.GET_TRADE_DETAIL_PATH_URL,
            data={"symbol": await self.exchange_symbol_associated_to_pair(order.trading_pair)},
            is_auth_required=True)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        try:
            if order.exchange_order_id is not None:
                all_fills_response = await self._request_order_fills(order=order)
                updates = self._create_order_fill_updates(order=order, fill_update=all_fills_response)
                trade_updates.extend(updates)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            is_error_caused_by_unexistent_order = '"code":50005' in str(ex)
            if not is_error_caused_by_unexistent_order:
                raise

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> Optional[OrderUpdate]:
        try:
            updated_order_data = await self._request_order_update(order=tracked_order)

            if not updated_order_data or "data" not in updated_order_data:
                self.logger().error(
                    f"Order status response missing 'data' for order {tracked_order.client_order_id}: {updated_order_data}"
                )
                return None

            order_data = updated_order_data["data"]
            state_field = order_data.get("order_state", order_data.get("state"))

            if state_field is None:
                self.logger().error(f"Missing 'order_state' or 'state' in response: {order_data}")
                return None

            # Log the raw data for debugging
            self.logger().info(f"Raw order status response for {tracked_order.client_order_id}: {order_data}")

            # Map the state to OrderState or log unknown states
            new_state = CONSTANTS.ORDER_STATE.get(str(state_field), OrderState.UNKNOWN)
            if new_state == OrderState.UNKNOWN:
                self.logger().warning(
                    f"Unknown state '{state_field}' encountered for order {tracked_order.client_order_id}. Response: {order_data}"
                )
                return None

            return OrderUpdate(
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=str(order_data.get("orderId", "UNKNOWN")),
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=new_state,
            )
        except Exception as e:
            self.logger().error(f"Error fetching status for order {tracked_order.client_order_id}: {e}", exc_info=True)
            return None

    def _create_order_fill_updates(self, order: InFlightOrder, fill_update: Dict[str, Any]) -> List[TradeUpdate]:
        updates = []
        try:
            # Handle when "data" is a list
            if isinstance(fill_update["data"], list):
                fills_data = fill_update["data"]
            elif "trades" in fill_update["data"]:
                fills_data = fill_update["data"]["trades"]
            else:
                raise KeyError("'data' or 'trades' not found in fill_update")
        except Exception as e:
            self.logger().error(f"Error in fill_update structure: {e}, fill_update: {fill_update}")
            return updates

        for fill_data in fills_data:
            trade_id = str(fill_data.get("tradeId"))

            # Ensure the trade has not been processed before
            if trade_id in order.processed_trade_ids:
                self.logger().info(f"Duplicate trade detected: {trade_id}, skipping...")
                continue

            order.processed_trade_ids.add(trade_id)

            fee = TradeFeeBase.new_spot_fee(
                fee_schema=self.trade_fee_schema(),
                trade_type=order.trade_type,
                percent_token=fill_data.get("feeCoinName", ""),
                flat_fees=[TokenAmount(amount=Decimal(fill_data.get("fee", "0")), token=fill_data.get("feeCoinName", ""))]
            )
            trade_update = TradeUpdate(
                trade_id=trade_id,
                client_order_id=order.client_order_id,
                exchange_order_id=str(fill_data.get("orderId")),
                trading_pair=order.trading_pair,
                fee=fee,
                fill_base_amount=Decimal(fill_data.get("size", "0")),
                fill_quote_amount=Decimal(fill_data.get("notional", "0")),
                fill_price=Decimal(fill_data.get("price", "0")),
                fill_timestamp=int(fill_data.get("createTime", 0)) * 1e-3,
            )
            updates.append(trade_update)

        return updates

    def _create_order_update(self, order: InFlightOrder, order_update: Dict[str, Any]) -> OrderUpdate:
        order_data = order_update["data"]
        new_state = CONSTANTS.ORDER_STATE[order_data["status"]]
        update = OrderUpdate(
            client_order_id=order.client_order_id,
            exchange_order_id=str(order_data["order_id"]),
            trading_pair=order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=new_state,
        )
        return update

    async def _user_stream_event_listener(self):
        async for event_message in self._iter_user_event_queue():
            try:
                # Log the incoming message
                self.logger().info(f"User stream event received: {event_message}")

                event_type = event_message.get("table")
                execution_data = event_message.get("data", [])

                if event_type == CONSTANTS.PRIVATE_ORDER_PROGRESS_CHANNEL_NAME:
                    for each_event in execution_data:
                        try:
                            client_order_id = each_event.get("client_order_id")
                            state_field = each_event.get("order_state", each_event.get("state"))

                            if state_field is None:
                                self.logger().warning(f"State field missing in event: {each_event}")
                                continue

                            new_state = CONSTANTS.ORDER_STATE.get(str(state_field))

                            if new_state is None:
                                self.logger().warning(f"Unmapped order state: {state_field}")
                                new_state = OrderState.UNKNOWN

                            updatable_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                            if updatable_order:
                                order_update = OrderUpdate(
                                    trading_pair=updatable_order.trading_pair,
                                    update_timestamp=self.current_timestamp,
                                    new_state=new_state,
                                    client_order_id=client_order_id,
                                    exchange_order_id=each_event.get("order_id"),
                                )
                                self.logger().info(f"Processing order update: {order_update}")
                                self._order_tracker.process_order_update(order_update=order_update)
                            else:
                                self.logger().info(
                                    f"Order {client_order_id} not tracked or already completed. Event: {each_event}"
                                )
                        except Exception as e:
                            self.logger().exception(
                                f"Error processing individual user stream event: {each_event}. Error: {e}"
                            )
            except Exception as e:
                self.logger().exception(f"Unexpected error in user stream event listener loop: {e}")

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(bitmart_utils.is_exchange_information_valid, exchange_info["data"]["symbols"]):
            mapping[symbol_data["symbol"]] = combine_to_hb_trading_pair(base=symbol_data["base_currency"],
                                                                        quote=symbol_data["quote_currency"])
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        params = {
            "symbol": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        }

        resp_json = await self._api_get(
            path_url=CONSTANTS.GET_LAST_TRADING_PRICES_PATH_URL,
            params=params
        )

        return float(resp_json["data"]["tickers"][0]["last_price"])