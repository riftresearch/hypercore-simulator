use crate::wire::EvmCall;
use serde_json::{Value, json};

const METHOD_NOT_FOUND: i64 = -32601;
const PARSE_ERROR: i64 = -32700;

pub fn answer(call: &EvmCall, base_fee: u64) -> Value {
    match call.method.as_str() {
        "eth_gasPrice" => {
            json!({"jsonrpc": "2.0", "id": call.id, "result": format!("{base_fee:#x}")})
        }
        method => error(
            &call.id,
            METHOD_NOT_FOUND,
            &format!("the method {method} does not exist/is not available"),
        ),
    }
}

pub fn unparsable() -> Value {
    error(&Value::Null, PARSE_ERROR, "parse error")
}

fn error(id: &Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(method: &str) -> EvmCall {
        serde_json::from_value(json!({"jsonrpc": "2.0", "id": 7, "method": method, "params": []}))
            .unwrap()
    }

    #[test]
    fn the_gas_price_is_the_set_small_block_base_fee_in_hex_wei() {
        assert_eq!(
            answer(&call("eth_gasPrice"), 6_767_000_000),
            json!({"jsonrpc": "2.0", "id": 7, "result": "0x1935839c0"})
        );
    }

    #[test]
    fn other_methods_and_unparsable_bodies_answer_json_rpc_errors() {
        let unknown = answer(&call("eth_sendRawTransaction"), 1);
        assert_eq!(unknown["error"]["code"], METHOD_NOT_FOUND);
        assert_eq!(unknown["id"], 7);
        assert_eq!(unparsable()["error"]["code"], PARSE_ERROR);
        assert_eq!(unparsable()["id"], Value::Null);
    }
}
