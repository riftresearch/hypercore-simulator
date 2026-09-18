use super::*;
use crate::wire::{Control, Envelope, Funding, Info, InfoRequest};
use serde_json::{Value, json};

const USDC_ONLY: &str = r#"{"tokens":[{"index":0,"name":"USDC","tokenId":"0x1","szDecimals":8,"weiDecimals":8}],"universe":[]}"#;
const NQ_MARKET: &str = r#"{
    "tokens":[
        {"index":0,"name":"USDC","tokenId":"0x1","szDecimals":8,"weiDecimals":8},
        {"index":1,"name":"NQ","tokenId":"0x2","szDecimals":2,"weiDecimals":10}
    ],
    "universe":[{"index":1302,"name":"@1302","tokens":[1,0]}]
}"#;

fn control(engine: &mut Engine, control: Control, raw: &Value, now: u64) -> Result<Value> {
    let raw = raw.to_string();
    let reply = engine.control(control, raw.as_bytes(), now)?;
    Ok(serde_json::to_value(reply).unwrap())
}

fn fund(engine: &mut Engine, body: Value, now: u64) -> Result<Value> {
    let funding: Funding = serde_json::from_value(body.clone()).unwrap();
    control(engine, Control::Fund(funding), &body, now)
}

fn account(engine: &mut Engine, address: &str, now: u64) -> Value {
    let body = json!({"address": address});
    let query = serde_json::from_value(body.clone()).unwrap();
    control(engine, Control::Account(query), &body, now).unwrap()
}

fn info(engine: &Engine, body: Value, now: u64) -> Value {
    let InfoRequest(query): InfoRequest = serde_json::from_value(body).unwrap();
    serde_json::to_value(engine.info(&query, now).unwrap()).unwrap()
}

fn exchange(engine: &mut Engine, mut envelope: Value, signer: &str, now: u64) -> Value {
    envelope["signature"] = json!({"r": "0x1", "s": "0x1", "v": 27});
    let raw = envelope.to_string();
    let parsed: Envelope = serde_json::from_str(&raw).unwrap();
    let reply = engine.exchange(
        &parsed,
        raw.as_bytes(),
        Address::parse(signer).unwrap(),
        now,
    );
    serde_json::to_value(reply).unwrap()
}

#[test]
fn unrepresentable_transfer_rejects_without_creating_money() {
    let mut engine = Engine::new(USDC_ONLY).unwrap();
    let sender = "0x1111111111111111111111111111111111111111";
    let recipient = "0x2222222222222222222222222222222222222222";
    fund(
        &mut engine,
        json!({"address":sender,"token":"USDC","amount":Decimal::MAX.to_string(),"mode":"transfer"}),
        1,
    )
    .unwrap();
    fund(
        &mut engine,
        json!({"address":recipient,"token":"USDC","amount":"1","mode":"transfer"}),
        1,
    )
    .unwrap();
    let before_sender = account(&mut engine, sender, 2);
    let before_recipient = account(&mut engine, recipient, 2);
    assert!(
        fund(
            &mut engine,
            json!({"address":recipient,"sender":sender,"token":"USDC","amount":"0.1","mode":"transfer"}),
            2,
        )
        .is_err()
    );
    assert_eq!(account(&mut engine, sender, 2), before_sender);
    assert_eq!(account(&mut engine, recipient, 2), before_recipient);
}

#[test]
fn activation_uses_usdt_without_shortchanging_recipient() {
    let mut engine = Engine::new(
        r#"{
            "tokens":[
                {"index":0,"name":"USDC","tokenId":"0x1","szDecimals":8,"weiDecimals":8},
                {"index":1,"name":"PURR","tokenId":"0x2","szDecimals":0,"weiDecimals":5},
                {"index":2,"name":"USDT","tokenId":"0x3","szDecimals":6,"weiDecimals":8}
            ],
            "universe":[{"index":0,"name":"PURR/USDT","tokens":[1,2]}]
        }"#,
    )
    .unwrap();
    let sender = "0x1111111111111111111111111111111111111111";
    let recipient = "0x2222222222222222222222222222222222222222";
    fund(
        &mut engine,
        json!({"address":sender,"token":"PURR","amount":"2","mode":"transfer"}),
        1,
    )
    .unwrap();
    fund(
        &mut engine,
        json!({"address":sender,"token":"USDT","amount":"1","mode":"transfer"}),
        1,
    )
    .unwrap();
    fund(
        &mut engine,
        json!({"address":recipient,"sender":sender,"token":"PURR","amount":"1","mode":"transfer"}),
        2,
    )
    .unwrap();
    let source = info(
        &engine,
        json!({"type":"spotClearinghouseState","user":sender}),
        2,
    );
    let balances = source["balances"].as_array().unwrap();
    assert_eq!(
        balances.iter().find(|row| row["coin"] == "USDT").unwrap()["total"],
        "0.0"
    );
    assert_eq!(
        balances.iter().find(|row| row["coin"] == "PURR").unwrap()["total"],
        "1.0"
    );
    let received = info(
        &engine,
        json!({"type":"spotClearinghouseState","user":recipient}),
        2,
    );
    assert_eq!(received["balances"][0]["total"], "1.0");
    let ledger = info(
        &engine,
        json!({"type":"userNonFundingLedgerUpdates","user":recipient}),
        2,
    );
    assert_eq!(ledger[0]["delta"]["feeToken"], "USDT");
    assert_eq!(ledger[0]["delta"]["fee"], "1.0");
}

#[test]
fn upgrade_rejection_admits_nonce_without_executing_order() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    let user = "0x1111111111111111111111111111111111111111";
    let now = 1_800_000_000_000_u64;
    fund(
        &mut engine,
        json!({"address":user,"token":"USDC","amount":"100","mode":"transfer"}),
        now,
    )
    .unwrap();
    let book =
        json!({"coin":"@1302","bids":[{"px":"0.99","sz":"100"}],"asks":[{"px":"1","sz":"100"}]});
    control(
        &mut engine,
        Control::Book(serde_json::from_value(book.clone()).unwrap()),
        &book,
        now,
    )
    .unwrap();
    let before = account(&mut engine, user, now);
    let upgrade = |engine: &mut Engine, post_only: bool| {
        let body = json!({"postOnly": post_only});
        control(
            engine,
            Control::Upgrade(serde_json::from_value(body.clone()).unwrap()),
            &body,
            now,
        )
        .unwrap();
    };
    upgrade(&mut engine, true);
    let mut envelope = json!({
        "nonce":now,"action":{"type":"order","grouping":"na","orders":[{
            "a":11302,"b":true,"p":"1","s":"20","r":false,"t":{"limit":{"tif":"Ioc"}}
        }]}
    });
    let rejected = exchange(&mut engine, envelope.clone(), user, now);
    assert_eq!(
        rejected["response"]["data"]["statuses"][0]["error"],
        "Only post-only orders allowed immediately after network upgrade"
    );
    let after = account(&mut engine, user, now);
    for field in ["state", "fills", "ledger", "orders", "cloids"] {
        assert_eq!(after[field], before[field], "{field}");
    }
    assert_eq!(after["userHasSentTx"], true);
    upgrade(&mut engine, false);
    assert_eq!(
        exchange(&mut engine, envelope.clone(), user, now),
        json!({"status":"err","response":format!("Invalid nonce: duplicate nonce {now}")})
    );
    envelope["nonce"] = json!(now + 1);
    let accepted = exchange(&mut engine, envelope, user, now);
    assert_eq!(
        accepted["response"]["data"]["statuses"][0]["filled"]["oid"],
        1
    );
    assert_eq!(
        accepted["response"]["data"]["statuses"][0]["filled"]["totalSz"],
        "20.0"
    );
}

#[test]
fn ioc_minimum_follows_crossing_and_nonzero_balance_at_execution_price() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    let user = "0x1111111111111111111111111111111111111111";
    let now = 1_800_000_000_000_u64;
    fund(
        &mut engine,
        json!({"address":user,"token":"USDC","amount":"0","mode":"transfer"}),
        now,
    )
    .unwrap();
    let book = json!({
        "coin":"@1302","markPx":"1.0211",
        "bids":[{"px":"1.0211","sz":"1000"}],
        "asks":[{"px":"1.024","sz":"1000"}]
    });
    control(
        &mut engine,
        Control::Book(serde_json::from_value(book.clone()).unwrap()),
        &book,
        now,
    )
    .unwrap();
    for (index, (size, price, funded, expected)) in [
        ("9.76", "1.1", false, "insufficientSpotBalanceRejected"),
        ("1", "0.9", false, "iocCancelRejected"),
        ("9.76", "1.1", true, "minTradeNtlRejected"),
        ("9.77", "1.1", false, "insufficientSpotBalanceRejected"),
        ("1", "0.9", false, "iocCancelRejected"),
    ]
    .into_iter()
    .enumerate()
    {
        if funded {
            fund(
                &mut engine,
                json!({"address":user,"token":"USDC","amount":"0.00000001","mode":"transfer"}),
                now,
            )
            .unwrap();
        }
        let client = format!("0x{index:032x}");
        let nonce = now + index as u64 + 1;
        exchange(
            &mut engine,
            json!({
                "nonce":nonce,"action":{"type":"order","grouping":"na","orders":[{
                    "a":11302,"b":true,"p":price,"s":size,"r":false,
                    "t":{"limit":{"tif":"Ioc"}},"c":client
                }]}
            }),
            user,
            nonce,
        );
        let state = info(
            &engine,
            json!({"type":"orderStatus","user":user,"oid":client}),
            nonce,
        );
        assert_eq!(
            state["order"]["status"], expected,
            "size={size}, price={price}"
        );
    }
    let balances = info(
        &engine,
        json!({"type":"spotClearinghouseState","user":user}),
        now,
    );
    assert_eq!(balances["balances"][0]["total"], "0.00000001");
}

#[test]
fn captured_aggregation_rounds_weighted_prices_and_omits_dust() {
    let mut fills: Vec<Fill> = serde_json::from_str(include_str!(
        "../../tests/fixtures/multilevel-fills-individual.json"
    ))
    .unwrap();
    fills.reverse();
    let expected: Value = serde_json::from_str(include_str!(
        "../../tests/fixtures/multilevel-fills-aggregated.json"
    ))
    .unwrap();
    assert_eq!(
        serde_json::to_value(aggregate_fills(&fills).unwrap()).unwrap(),
        expected
    );
}

#[test]
fn captured_aggregation_caps_groups_after_combining_individual_fills() {
    let recent: Vec<Fill> =
        serde_json::from_str(include_str!("../../tests/fixtures/maker-fills-false.json")).unwrap();
    let cutoff = recent.last().unwrap().time;
    let mut complete: Vec<Fill> =
        serde_json::from_str(include_str!("../../tests/fixtures/older-maker-fills.json")).unwrap();
    complete.extend(recent.into_iter().rev().filter(|fill| fill.time > cutoff));
    let expected: Value =
        serde_json::from_str(include_str!("../../tests/fixtures/maker-fills-true.json")).unwrap();
    assert_eq!(
        serde_json::to_value(aggregate_fills(&complete).unwrap()).unwrap(),
        expected
    );
}

#[test]
fn bundled_metadata_loads_for_both_networks() {
    for (label, meta) in [
        ("mainnet", MAINNET_SPOT_META),
        ("testnet", TESTNET_SPOT_META),
    ] {
        let engine = Engine::new(meta).unwrap_or_else(|error| panic!("{label}: {error}"));
        assert!(engine.pair_by_name("PURR/USDC").is_some(), "{label}");
    }
}

#[test]
fn spot_meta_is_echoed_verbatim() {
    let engine = Engine::new(NQ_MARKET).unwrap();
    let reply = engine.info(&Info::SpotMeta, 1).unwrap();
    let text = serde_json::to_string(&reply).unwrap();
    assert_eq!(text, NQ_MARKET.trim());
}

#[test]
fn aggregated_books_report_the_raw_spread() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    let query = |engine: &Engine, sig: Option<u8>| {
        let query = Info::L2Book {
            coin: "@1302".into(),
            n_sig_figs: sig,
            mantissa: None,
        };
        serde_json::to_value(engine.info(&query, 1).unwrap()).unwrap()
    };
    // Empty or one-sided books have no measured spread, so none is reported.
    assert!(query(&engine, Some(5)).get("spread").is_none());
    let book =
        json!({"coin":"@1302","bids":[{"px":"4.5795","sz":"1"}],"asks":[{"px":"4.6252","sz":"1"}]});
    control(
        &mut engine,
        Control::Book(serde_json::from_value(book.clone()).unwrap()),
        &book,
        1,
    )
    .unwrap();
    assert_eq!(query(&engine, Some(2))["spread"], "0.0457");
    assert!(query(&engine, None).get("spread").is_none());
}

// ---------------------------------------------------------------------------
// Resting orders, cancels, modifies and triggers. These semantics follow the
// documented API and the recovered matching loop, not testnet measurements.

const MAKER: &str = "0x1111111111111111111111111111111111111111";
const TAKER: &str = "0x2222222222222222222222222222222222222222";
const NOW: u64 = 1_800_000_000_000;

fn order(tif: &str, buy: bool, px: &str, sz: &str) -> Value {
    json!({"a":11302,"b":buy,"p":px,"s":sz,"r":false,"t":{"limit":{"tif":tif}}})
}

fn submit(engine: &mut Engine, signer: &str, nonce: u64, action: Value) -> Value {
    exchange(
        engine,
        json!({"nonce": nonce, "action": action}),
        signer,
        NOW,
    )
}

fn orders(engine: &mut Engine, signer: &str, nonce: u64, orders: Vec<Value>) -> Value {
    submit(
        engine,
        signer,
        nonce,
        json!({"type":"order","grouping":"na","orders":orders}),
    )
}

fn balance(engine: &Engine, user: &str, coin: &str) -> (String, String) {
    let state = info(
        engine,
        json!({"type":"spotClearinghouseState","user":user}),
        NOW,
    );
    let row = state["balances"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["coin"] == coin)
        .cloned()
        .unwrap_or(json!({"total":"0.0","hold":"0.0"}));
    (
        row["total"].as_str().unwrap().to_owned(),
        row["hold"].as_str().unwrap().to_owned(),
    )
}

fn status(engine: &Engine, user: &str, oid: u64) -> Value {
    info(
        engine,
        json!({"type":"orderStatus","user":user,"oid":oid}),
        NOW,
    )["order"]["status"]
        .clone()
}

fn market(engine: &mut Engine) {
    fund(
        &mut *engine,
        json!({"address":MAKER,"token":"USDC","amount":"1000","mode":"transfer"}),
        NOW,
    )
    .unwrap();
    fund(
        &mut *engine,
        json!({"address":MAKER,"token":"NQ","amount":"100","mode":"transfer"}),
        NOW,
    )
    .unwrap();
    fund(
        &mut *engine,
        json!({"address":TAKER,"token":"USDC","amount":"1000","mode":"transfer"}),
        NOW,
    )
    .unwrap();
    fund(
        &mut *engine,
        json!({"address":TAKER,"token":"NQ","amount":"100","mode":"transfer"}),
        NOW,
    )
    .unwrap();
    let book = json!({"coin":"@1302","markPx":"1.0","bids":[{"px":"0.9","sz":"1000"}],"asks":[{"px":"1.1","sz":"1000"}]});
    control(
        engine,
        Control::Book(serde_json::from_value(book.clone()).unwrap()),
        &book,
        NOW,
    )
    .unwrap();
}

#[test]
fn gtc_order_rests_with_a_hold_and_fills_as_maker() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    // Maker bids 30 at 1.0: inside the spread, so it rests and locks 30 USDC.
    let rested = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![order("Gtc", true, "1", "30")],
    );
    let oid = rested["response"]["data"]["statuses"][0]["resting"]["oid"]
        .as_u64()
        .unwrap();
    assert_eq!(
        balance(&engine, MAKER, "USDC"),
        ("1000.0".into(), "30.0".into())
    );
    assert_eq!(status(&engine, MAKER, oid), "open");
    let open = info(&engine, json!({"type":"openOrders","user":MAKER}), NOW);
    assert_eq!(open[0]["oid"], oid);
    assert_eq!(open[0]["limitPx"], "1.0");
    let frontend = info(
        &engine,
        json!({"type":"frontendOpenOrders","user":MAKER}),
        NOW,
    );
    assert_eq!(frontend[0]["tif"], "Gtc");
    assert_eq!(frontend[0]["orderType"], "Limit");
    let book = info(&engine, json!({"type":"l2Book","coin":"@1302"}), NOW);
    assert_eq!(book["levels"][0][0], json!({"px":"1.0","sz":"30.0","n":1}));

    // Taker sells 12 at 0.95: crosses the maker before the synthetic 0.9 level.
    let filled = orders(
        &mut engine,
        TAKER,
        NOW + 2,
        vec![order("Ioc", false, "0.95", "12")],
    );
    let fill = &filled["response"]["data"]["statuses"][0]["filled"];
    assert_eq!(fill["avgPx"], "1.0");
    assert_eq!(fill["totalSz"], "12.0");
    // The maker's order shrank, its hold released, and it received NQ less a maker fee.
    assert_eq!(
        balance(&engine, MAKER, "USDC"),
        ("988.0".into(), "18.0".into())
    );
    assert_eq!(status(&engine, MAKER, oid), "open");
    let maker_fills = info(&engine, json!({"type":"userFills","user":MAKER}), NOW);
    assert_eq!(maker_fills[0]["crossed"], false);
    assert_eq!(maker_fills[0]["side"], "B");
    assert_eq!(maker_fills[0]["sz"], "12.0");
    let taker_fills = info(&engine, json!({"type":"userFills","user":TAKER}), NOW);
    assert_eq!(taker_fills[0]["crossed"], true);
    assert_eq!(taker_fills[0]["tid"], maker_fills[0]["tid"]);
    let (nq_total, _) = balance(&engine, MAKER, "NQ");
    let nq: Decimal = nq_total.parse().unwrap();
    assert!(
        nq > Decimal::from(111) && nq < Decimal::from(112),
        "{nq_total}"
    );

    // Selling the rest fills the maker order completely.
    orders(
        &mut engine,
        TAKER,
        NOW + 3,
        vec![order("Ioc", false, "0.95", "18")],
    );
    assert_eq!(status(&engine, MAKER, oid), "filled");
    assert_eq!(balance(&engine, MAKER, "USDC").1, "0.0");
    assert!(
        info(&engine, json!({"type":"openOrders","user":MAKER}), NOW)
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn post_only_orders_reject_when_they_would_cross() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let crossing = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![order("Alo", true, "1.1", "20")],
    );
    assert_eq!(
        crossing["response"]["data"]["statuses"][0]["error"],
        "Post only order would have immediately matched, bbo was 0.9 @ 1.1. asset=11302"
    );
    assert_eq!(status(&engine, MAKER, 1), "badAloPxRejected");
    let resting = orders(
        &mut engine,
        MAKER,
        NOW + 2,
        vec![order("Alo", true, "1", "20")],
    );
    assert!(resting["response"]["data"]["statuses"][0]["resting"]["oid"].is_u64());
}

#[test]
fn resting_orders_need_full_funding_and_ioc_keeps_partial_behavior() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let rejected = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![order("Gtc", true, "1", "2000")],
    );
    assert_eq!(
        rejected["response"]["data"]["statuses"][0]["error"],
        "Insufficient spot balance asset=11302"
    );
    // A transfer cannot spend locked funds.
    orders(
        &mut engine,
        MAKER,
        NOW + 2,
        vec![order("Gtc", true, "1", "990")],
    );
    let send = fund(
        &mut engine,
        json!({"address":TAKER,"sender":MAKER,"token":"USDC","amount":"50","mode":"transfer"}),
        NOW,
    );
    assert_eq!(
        send.unwrap_err().as_str(),
        "Insufficient balance for token transfer"
    );
}

#[test]
fn cancels_release_holds_and_report_missing_orders() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let cloid = "0x000000000000000000000000000000ab";
    let mut with_cloid = order("Gtc", true, "1", "20");
    with_cloid["c"] = json!(cloid);
    let rested = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![with_cloid, order("Gtc", false, "1.05", "10")],
    );
    let statuses = rested["response"]["data"]["statuses"]
        .as_array()
        .unwrap()
        .clone();
    let bid = statuses[0]["resting"]["oid"].as_u64().unwrap();
    let ask = statuses[1]["resting"]["oid"].as_u64().unwrap();
    assert_eq!(balance(&engine, MAKER, "NQ").1, "10.0");
    let canceled = submit(
        &mut engine,
        MAKER,
        NOW + 2,
        json!({"type":"cancel","cancels":[{"a":11302,"o":ask},{"a":11302,"o":999}]}),
    );
    assert_eq!(canceled["response"]["type"], "cancel");
    assert_eq!(canceled["response"]["data"]["statuses"][0], "success");
    assert_eq!(
        canceled["response"]["data"]["statuses"][1]["error"],
        "Order 999: Order was never placed, already canceled, or filled."
    );
    assert_eq!(status(&engine, MAKER, ask), "canceled");
    assert_eq!(balance(&engine, MAKER, "NQ").1, "0.0");
    let by_cloid = submit(
        &mut engine,
        MAKER,
        NOW + 3,
        json!({"type":"cancelByCloid","cancels":[{"asset":11302,"cloid":cloid}]}),
    );
    assert_eq!(by_cloid["response"]["data"]["statuses"][0], "success");
    assert_eq!(status(&engine, MAKER, bid), "canceled");
    assert_eq!(balance(&engine, MAKER, "USDC").1, "0.0");
    let history = info(
        &engine,
        json!({"type":"historicalOrders","user":MAKER}),
        NOW,
    );
    assert_eq!(history.as_array().unwrap().len(), 2);
    assert_eq!(history[0]["order"]["oid"], ask);
}

#[test]
fn modify_replaces_an_order_under_a_new_oid() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let rested = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![order("Gtc", true, "1", "20")],
    );
    let old = rested["response"]["data"]["statuses"][0]["resting"]["oid"]
        .as_u64()
        .unwrap();
    let modified = submit(
        &mut engine,
        MAKER,
        NOW + 2,
        json!({"type":"modify","oid":old,"order":order("Gtc", true, "1.05", "30")}),
    );
    assert_eq!(
        modified,
        json!({"status":"ok","response":{"type":"default"}})
    );
    assert_eq!(status(&engine, MAKER, old), "canceled");
    let open = info(&engine, json!({"type":"openOrders","user":MAKER}), NOW);
    assert_eq!(open.as_array().unwrap().len(), 1);
    assert_eq!(open[0]["limitPx"], "1.05");
    assert_eq!(open[0]["sz"], "30.0");
    assert_eq!(balance(&engine, MAKER, "USDC").1, "31.5");
    let batch = submit(
        &mut engine,
        MAKER,
        NOW + 3,
        json!({"type":"batchModify","modifies":[
            {"oid": open[0]["oid"], "order": order("Gtc", true, "1.02", "10")},
            {"oid": old, "order": order("Gtc", true, "1.02", "10")}
        ]}),
    );
    let statuses = &batch["response"]["data"]["statuses"];
    assert!(statuses[0]["resting"]["oid"].is_u64());
    assert_eq!(
        statuses[1]["error"],
        "Cannot modify canceled or filled order."
    );
}

#[test]
fn self_trade_cancels_the_resting_order() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let rested = orders(
        &mut engine,
        MAKER,
        NOW + 1,
        vec![order("Gtc", false, "1.05", "10")],
    );
    let oid = rested["response"]["data"]["statuses"][0]["resting"]["oid"]
        .as_u64()
        .unwrap();
    // The maker's own buy crosses only its resting ask, which is canceled instead of matched.
    let crossing = orders(
        &mut engine,
        MAKER,
        NOW + 2,
        vec![order("Ioc", true, "1.05", "10")],
    );
    assert_eq!(
        crossing["response"]["data"]["statuses"][0]["error"],
        "Order could not immediately match against any resting orders. asset=11302"
    );
    assert_eq!(status(&engine, MAKER, oid), "selfTradeCanceled");
    assert_eq!(
        balance(&engine, MAKER, "NQ"),
        ("100.0".into(), "0.0".into())
    );
}

#[test]
fn stop_market_orders_fire_on_the_reference_price() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let stop = json!({"a":11302,"b":false,"p":"0.85","s":"20","r":false,
        "t":{"trigger":{"isMarket":true,"triggerPx":"0.95","tpsl":"sl"}}});
    let rested = orders(&mut engine, MAKER, NOW + 1, vec![stop]);
    let oid = rested["response"]["data"]["statuses"][0]["resting"]["oid"]
        .as_u64()
        .unwrap();
    let frontend = info(
        &engine,
        json!({"type":"frontendOpenOrders","user":MAKER}),
        NOW,
    );
    assert_eq!(frontend[0]["isTrigger"], true);
    assert_eq!(frontend[0]["orderType"], "Stop Market");
    assert_eq!(frontend[0]["triggerCondition"], "Price below 0.95");
    assert_eq!(frontend[0]["triggerPx"], "0.95");
    // Untriggered orders lock nothing.
    assert_eq!(balance(&engine, MAKER, "NQ").1, "0.0");
    // A take-profit that would fire immediately is rejected.
    let bad = json!({"a":11302,"b":false,"p":"1.2","s":"10","r":false,
        "t":{"trigger":{"isMarket":false,"triggerPx":"0.95","tpsl":"tp"}}});
    let rejected = orders(&mut engine, MAKER, NOW + 2, vec![bad]);
    assert_eq!(
        rejected["response"]["data"]["statuses"][0]["error"],
        "Invalid TP/SL price. asset=11302"
    );
    // Moving the mark below the trigger fires the stop into the synthetic bids.
    let mark = json!({"coin":"@1302","markPx":"0.9"});
    control(
        &mut engine,
        Control::Book(serde_json::from_value(mark.clone()).unwrap()),
        &mark,
        NOW + 3,
    )
    .unwrap();
    assert_eq!(status(&engine, MAKER, oid), "filled");
    let fills = info(&engine, json!({"type":"userFills","user":MAKER}), NOW);
    assert_eq!(fills[0]["px"], "0.9");
    assert_eq!(fills[0]["oid"], oid);
    assert_eq!(balance(&engine, MAKER, "NQ").0, "80.0");
}

#[test]
fn scheduled_cancel_is_volume_gated_and_cancels_open_orders() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    let gated = submit(
        &mut engine,
        MAKER,
        NOW + 1,
        json!({"type":"scheduleCancel","time":NOW + 10_000}),
    );
    assert_eq!(
        gated["response"],
        "Cannot set scheduled cancel time until enough volume traded"
    );
    orders(
        &mut engine,
        MAKER,
        NOW + 2,
        vec![order("Gtc", true, "1", "20")],
    );
    // Trade past the gate, then arm the switch and let the clock pass it.
    let mut nonce = NOW + 3;
    for _ in 0..3 {
        let book = json!({"coin":"@1302","bids":[{"px":"0.9","sz":"1000"}],"asks":[{"px":"1.1","sz":"500000"}]});
        control(
            &mut engine,
            Control::Book(serde_json::from_value(book.clone()).unwrap()),
            &book,
            NOW,
        )
        .unwrap();
        fund(
            &mut engine,
            json!({"address":TAKER,"token":"USDC","amount":"500000","mode":"transfer"}),
            NOW,
        )
        .unwrap();
        orders(
            &mut engine,
            TAKER,
            nonce,
            vec![order("Ioc", true, "1.1", "400000")],
        );
        nonce += 1;
    }
    let taker_lifetime = engine.accounts[&Address::parse(TAKER).unwrap()].lifetime_volume;
    assert!(
        taker_lifetime >= Decimal::from(1_000_000),
        "{taker_lifetime}"
    );
    let too_soon = submit(
        &mut engine,
        TAKER,
        nonce,
        json!({"type":"scheduleCancel","time":NOW + 1000}),
    );
    assert_eq!(
        too_soon["response"],
        "Scheduled cancel time too early, must be at least 5 seconds from now."
    );
    orders(
        &mut engine,
        TAKER,
        nonce + 1,
        vec![order("Gtc", true, "1", "10")],
    );
    let armed = submit(
        &mut engine,
        TAKER,
        nonce + 2,
        json!({"type":"scheduleCancel","time":NOW + 10_000}),
    );
    assert_eq!(armed["status"], "ok");
    engine.advance(NOW + 20_000, false).unwrap();
    let history = info(
        &engine,
        json!({"type":"historicalOrders","user":TAKER}),
        NOW,
    );
    assert_eq!(history[0]["status"], "scheduledCancel");
    assert!(
        info(&engine, json!({"type":"openOrders","user":TAKER}), NOW)
            .as_array()
            .unwrap()
            .is_empty()
    );
    // The maker's untouched order survives; only the armed account was swept.
    assert_eq!(
        info(&engine, json!({"type":"openOrders","user":MAKER}), NOW)
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn fills_by_time_are_inclusive_and_oldest_first() {
    let mut engine = Engine::new(NQ_MARKET).unwrap();
    market(&mut engine);
    // Three taker buys at distinct clocks; each is one fill against the synthetic ask.
    for (index, at) in [NOW + 1000, NOW + 2000, NOW + 3000].into_iter().enumerate() {
        let envelope = json!({"nonce": NOW + 10 + index as u64,
            "action": {"type":"order","grouping":"na","orders":[order("Ioc", true, "1.1", "10")]}});
        exchange(&mut engine, envelope, TAKER, at);
    }
    let times = |body: Value| -> Vec<u64> {
        info(&engine, body, NOW + 5000)
            .as_array()
            .unwrap()
            .iter()
            .map(|fill| fill["time"].as_u64().unwrap())
            .collect()
    };
    let all = times(json!({"type":"userFillsByTime","user":TAKER,"startTime":NOW}));
    assert_eq!(all, vec![NOW + 1000, NOW + 2000, NOW + 3000]);
    let bounded = times(
        json!({"type":"userFillsByTime","user":TAKER,"startTime":NOW + 2000,"endTime":NOW + 3000}),
    );
    assert_eq!(bounded, vec![NOW + 2000, NOW + 3000]);
    let null_end =
        times(json!({"type":"userFillsByTime","user":TAKER,"startTime":NOW + 3000,"endTime":null}));
    assert_eq!(null_end, vec![NOW + 3000]);
    let aggregated = times(
        json!({"type":"userFillsByTime","user":TAKER,"startTime":NOW,"aggregateByTime":true}),
    );
    assert_eq!(aggregated, vec![NOW + 1000, NOW + 2000, NOW + 3000]);
    let newest_first = times(json!({"type":"userFills","user":TAKER}));
    assert_eq!(newest_first, vec![NOW + 3000, NOW + 2000, NOW + 1000]);
    assert!(
        serde_json::from_value::<InfoRequest>(json!({"type":"userFillsByTime","user":TAKER}))
            .is_err()
    );
    assert!(
        serde_json::from_value::<InfoRequest>(
            json!({"type":"userFillsByTime","user":TAKER,"startTime":1,"aggregateByTime":null})
        )
        .is_err()
    );
}
