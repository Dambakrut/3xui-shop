# Synthetic v3.9.0 fixtures

These are minimal schema examples, not captured production data.
Compared official tag commit 3cd4bf504c3cd8ea9b1c1fdb032a9796c5c43ddb.

| Fixture | Official source |
|---|---|
| client.json | internal/web/controller/client.go buildClientPayload; database/model/model.go ClientRecord |
| traffic.json | internal/xray/client_traffic.go |
| inbound_xhttp.json / inbounds.json | database/model/model.go Inbound; controller/inbound.go list |
| server_status.json | controller/server.go status; service/server.go Status/CurrentStatus |
| settings.json | controller/setting.go all; web/entity/entity.go AllSetting |
| sub_links.json | controller/client.go getSubLinks; internal/sub/service.go |
| mutation_success.json / mutation_pending.json | controller/util.go pendingNodeObj/jsonMsgObj; controller/client.go create/update/delete |

All paths are relative to https://github.com/MHSanaei/3x-ui/tree/v3.9.0/.
UUID/email/host/subId values are synthetic. Inbound settings are a minimal
supported VLESS Reality XHTTP policy fixture, not a usable Xray configuration.
Client resetWeekday=3 represents a preserved Wednesday weekly schedule; it
does not instruct the shop to enable weekly auto-renew for new purchases.
Optional newly added create traffic import data is deliberately omitted.
Historical v3.8.5 fixtures are retained.

Share-link fixture strings use a redacted synthetic placeholder. Complete URI
validation is tested with dynamically generated credentials on loopback servers.
