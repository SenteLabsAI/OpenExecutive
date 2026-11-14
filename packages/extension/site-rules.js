// Open Executive browser extension: the site lists. Change them here to fit
// your company, then rebuild (node build.mjs) or reload the unpacked folder.
// Loaded first, before shared.js, on every page, by the popup and by
// background.js. Plain script (no modules): it sets globalThis.OE_SITE_RULES.
//
// Hosts match themselves and their subdomains ("slack.com" also covers
// "acme.slack.com").

globalThis.OE_SITE_RULES = {
  // Email and chat sites: a click or typing there can send a message, so it
  // goes only when the person's Approvals let it (or they approve its card).
  sendingHosts: [
    "mail.google.com", "chat.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com",
    "outlook.cloud.microsoft", "teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "slack.com",
    "web.telegram.org", "discord.com", "web.whatsapp.com", "messenger.com", "mail.yahoo.com", "mail.proton.me",
    "mail.zoho.com", "app.hey.com", "superhuman.com", "messages.google.com", "voice.google.com",
    "calendar.google.com", "discordapp.com", "mail.protonmail.com", "mail.aol.com", "fastmail.com",
    "web.skype.com", "outlook.office365.us", "teams.microsoft.us",
    // Social sites with chat boxes that open on any page.
    "linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
  ],

  // The same, for a site where only part of it sends: [host, path prefix].
  sendingPaths: [["icloud.com", "/mail"]],

  // Payment providers whose card fields a shop shows in a frame: screenshots
  // black those frames out.
  paymentFrameHosts: [
    "stripe.com", "stripe.network", "braintreegateway.com", "braintree-api.com", "adyen.com", "adyenpayments.com",
    "paypal.com", "checkout.com", "squareup.com", "squarecdn.com", "recurly.com", "chargebee.com", "authorize.net",
    "spreedly.com", "bluesnap.com", "worldpay.com", "cybersource.com", "klarna.com", "affirm.com",
  ],
};
