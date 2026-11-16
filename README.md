# Cosmos Hub Proposal Tracker

This runs once a day as a GitHub Action. It checks **cosmoshub-4** for governance proposals 
that are in the deposit or voting period and posts each one to a Telegram channel. 
The **proposal type** (the on-chain message that actually runs if the proposal passes) is the header of every message.

## Why

On [2026-09-22 Neutron was exploited](https://forum.cosmos.network/t/discussion-move-the-1-227-121-atom-recovered-after-the-neutron-exploit-to-the-neutron-recovery-multisig/17385) through governance. 
In [Neutron proposal #9](https://www.mintscan.io/neutron/proposals/9), the title and summary 
looked harmless, but the actual message was `/cosmwasm.wasm.v1.MsgUpdateAdmin`. 
People who read only the description missed it. This tool puts the real message type first so that kind of mismatch is easy to spot.

## Example alert

```
🚨 proposal_type:
/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade

Cosmos Hub #1061 - Gaia v29.1.0 upgrade

🚨 HIGH-RISK message type(s): /cosmos.upgrade.v1beta1.MsgSoftwareUpgrade

proposal_status: PROPOSAL_STATUS_VOTING_PERIOD
submit_time: 2026-11-10 11:54:43 UTC
deposit_end_time: 2026-11-24 11:54:43 UTC
voting_start_time: 2026-11-10 11:57:22 UTC
voting_end_time: 2026-11-17 11:57:22 UTC (in 1d 8h)
proposer: cosmos1az7rk9kukju026t0mz8jlus924hj7kqrlqz0ye

summary:
# Overview ... (truncated to 700 chars)

Mintscan | ping.pub
```

Header icons:

| Icon  | Meaning                                                                                                                                                         |
|-------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 📝    | `Text` proposal. Nothing runs on-chain                                                                                                                          |
| ⚠️    | Runs on-chain messages. Check that they match the description                                                                                                   |
| 🚨    | Runs a high-risk message: CosmWasm admin or code changes, fund transfers, community pool spends, upgrades, IBC client changes, any `MsgUpdateParams`, or authz  |

Extra 🚩 flags:
- The title or summary says "signaling", "text proposal", "no on-chain" and similar, but the proposal runs messages.
- The proposal uses `authz MsgExec`, so it acts on behalf of another account.

Wrapper messages are expanded to show what really runs. 
For example, `MsgExec → /cosmos.bank.v1beta1.MsgSend` or `MsgExecLegacyContent → /cosmos.gov.v1beta1.TextProposal`.

## Behavior

- **Schedule:** daily at 01:00 UTC, set in `.github/workflows/hub-proposal-tracker.yml`. Daily is enough because voting lasts 7 days 
  and expedited voting lasts 3 days. Also, can run manually.
- **What is posted:** one message per proposal in `VOTING_PERIOD` or `DEPOSIT_PERIOD`, with voting proposals first. 
  A proposal is posted every day it stays active, so it acts as a daily reminder until voting ends.
- **No active proposals:** nothing is posted.
- **Endpoint failover:** the endpoints are tried in order. Before an endpoint is used, its chain-id is checked to be `cosmoshub-4`.
  - If an endpoint fails and a later one works, a ⚠️ warning is posted to Telegram. Set `NOTIFY_ON_FAILOVER=false` to turn this off.
  - If all endpoints fail, a ❌ error listing each failure is posted and the run is marked failed.
- **Telegram failures:** each send is retried and Telegram's rate limits are respected. 
  If an HTML message is rejected, it is resent as plain text. If it still fails, a ❌ error notice is posted. 
  If the script crashes, or a workflow step fails before the script can report it, 
  the last workflow step sends a ❌ notice with a link to the run. If Telegram is unreachable altogether, 
  the run is marked failed and GitHub emails the repo owner.

### About "RPC" vs REST

The tool uses the Cosmos SDK **REST (LCD) API** (`/cosmos/gov/v1/proposals`) on public endpoints. 
It returns plain JSON, including the message `@type`. Tendermint RPC (port 26657) returns protobuf bytes 
for governance queries, which would need protobuf decoding. That's why REST is used.
Default public endpoints:

```
https://cosmos-rest.publicnode.com
https://rest.cosmos.directory/cosmoshub
https://cosmos-api.polkachu.com
https://rest-cosmoshub.ecostake.com
https://api.cosmos.nodestake.org
```

Can replace the list without editing code. Set the repository variable `COSMOS_REST_ENDPOINTS` to a comma-separated list.

> [!IMPORTANT]
> **60-day inactivity rule:** GitHub turns off scheduled workflows in public repos after 60 days with
> no repository activity, such as commits. Before it does, GitHub emails the repo owner.
