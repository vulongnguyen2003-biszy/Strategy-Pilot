# Strategy-Pilot

Claude chạy **paper trading** chiến lược sell OTM BTC options trên data live của Deribit. Không đặt lệnh thật.
Một cloud routine chạy mỗi 2 giờ, thêm các lần 09:10 và 15:10 giờ VN. Mỗi lần chạy, routine gọi `python pilot.py run`, phán đoán phần được phép, rồi commit kết quả lên `main`.

- Rule: [RULES.md](RULES.md)
- Tham số: [config.json](config.json)
- Prompt của routine: [ROUTINE.md](ROUTINE.md)

## File
| File | Nội dung |
|---|---|
| `state.json` | Trạng thái hiện tại: tiền mặt USD, lot đang mở, trạng thái cycle (số lần exit 2, đã dừng, roll đang chờ), quota entry theo ngày, suggestion đang chờ duyệt, override (pause/leverage...) |
| `logs/trade-log.jsonl` | Log append-only, 1 record/dòng. Mọi record đều có `type`, `run_id`, `ts_utc`, `ts_vn` |

### Các loại record
| type | Khi nào | Field chính |
|---|---|---|
| `obs` | Mọi lần chạy | index, dvol, nến 1H gần nhất, range 1H %, positions (mark/bid/ask/delta/IV/DTE/ngưỡng exit 2/lãi lỗ chưa chốt), equity, leverage_used, std_margin_est_pct |
| `trade` | Mở hoặc đóng | action, lot_id, instrument, side, qty, price_btc, price_basis (bid/ask/mark), index, premium_usd / cost_usd, fee_usd, pnl_usd, cycle, leg (initial/roll), rule, source (rule/claude/user/user_approved), reason, ref |
| `expiry` | Lot hết hạn | delivery_price, itm, cost_usd, fee_usd, pnl_usd |
| `flag` | Có gì đáng chú ý | code: near_exit2, spot_move, roll_wait, roll_cancelled, stop_roll, delta_out_of_band, no_room, no_candidate, cycle_not_listed, settlement_pending |
| `decision` | Claude chọn chờ hoặc vào ở khung không phải khung cuối | text (rationale) |
| `suggestion` | Claude đề xuất ngoài rule | id, text, rationale, order |
| `suggestion_expired` | Suggestion không được duyệt trước lần chạy kế tiếp | id |
| `user_cmd` / `answer` | Lệnh hoặc trả lời của bạn trong phiên | text |
| `roll_wait` | Vẫn đang chờ roll (từ lần thứ 2 trở đi) | range_1h |

## Lệnh
```bash
python pilot.py run                     # lần chạy định kỳ: exit → roll → entry → flag → obs
python pilot.py status                  # xem vị thế và equity theo giá live
python pilot.py trade open --side put --qty auto --source claude --rule R-E2 --reason "..."
python pilot.py trade close --lot L-3 --source user --reason "..."
python pilot.py trade close --all --source user --reason "đóng hết"
python pilot.py note --type suggestion --text "..." --data '{"rationale":"...","order":"..."}'
python pilot.py set paused.call true    # pause/resume, hoặc ghi đè tham số config (vd leverage_per_side 6)
python pilot.py --dry --now 2026-11-11T12:10+07:00 run   # test, không ghi file
```
