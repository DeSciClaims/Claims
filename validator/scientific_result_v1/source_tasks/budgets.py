"""Owner-approved validator review allowances, frozen in every scoring policy."""

SOURCE_TOKEN_BUDGETS = (128000, 128000, 128000)
STRUCTURED_PACKET_TOKENS = 64000
# Two context retrievals remain allowed. The complete rendered request, including
# instructions and output reservation, must still fit each frozen model route.
