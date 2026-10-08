export class SlackIdentityError extends Error {
  constructor(message) {
    super(message);
    this.name = 'SlackIdentityError';
  }
}

export function assertSlackIdentity(auth, expectedTeamId, expectedBotUserId) {
  const teamId = String(auth?.team_id || '');
  const botUserId = String(auth?.user_id || '');

  if (!expectedTeamId || !expectedBotUserId) {
    throw new SlackIdentityError(
      'HERMES_SLACK_TEAM_ID and HERMES_SLACK_BOT_USER_ID are required',
    );
  }
  if (teamId !== expectedTeamId) {
    throw new SlackIdentityError(
      `Slack team mismatch: expected ${expectedTeamId}, got ${teamId || '(empty)'}`,
    );
  }
  if (botUserId !== expectedBotUserId) {
    throw new SlackIdentityError(
      `Slack bot user mismatch: expected ${expectedBotUserId}, got ${botUserId || '(empty)'}`,
    );
  }
}
