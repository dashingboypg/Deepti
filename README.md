# Deepti — ETHUSD Ladder Bot

Production bot:
- Symbol: ETHUSD
- Product ID: 3136
- Buy spacing: $1
- Take-profit: $3
- Order size: 1 lot
- Maximum position: 45 lots
- Credentials: AWS Secrets Manager (`Dipti`)
- PM2 process: `dipti-ladder`

Production path:
`/home/ec2-user/delta-bot/dipti-ladder/`

State file:
`dipti_ladder_state.json`

The bot must obtain API credentials from AWS Secrets Manager.
No credentials are stored in this repository.

Recovery:
1. Restore `dipti_ladder_bot.py`
2. Restore `dipti_ladder_state.json`
3. Ensure the AWS secret `Dipti` exists
4. Verify the Delta API credentials
5. Start the bot with PM2 as `dipti-ladder`
