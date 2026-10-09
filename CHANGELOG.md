# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From 0.3.0 on, entries are written by release-please from the titles of the
merged pull requests (`feat` → Added, `fix` → Fixed). The open release PR holds
the next entry; edit it there before merging if a line needs rewording.

## [0.5.3](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.5.2...v0.5.3) (2026-10-09)


### Added

* **agents:** refresh the default models ([#390](https://github.com/SenteLabsAI/OpenExecutive/issues/390)) ([b747bee](https://github.com/SenteLabsAI/OpenExecutive/commit/b747bee881f733792608a5a445fb34ff3fff5288))
* **chat:** name connected tools in plain words and collapse repeated chips ([#394](https://github.com/SenteLabsAI/OpenExecutive/issues/394)) ([1ce8d97](https://github.com/SenteLabsAI/OpenExecutive/commit/1ce8d972b3f2b3a5ec496750321f5e2c5a7a9505))
* **delegation:** add an In training step to Handle it for me ([#403](https://github.com/SenteLabsAI/OpenExecutive/issues/403)) ([f581e8c](https://github.com/SenteLabsAI/OpenExecutive/commit/f581e8c4cd4092c9e02948e5115bd956815a464b))
* **delegation:** add reminders and forward-as-draft to Act as me ([#387](https://github.com/SenteLabsAI/OpenExecutive/issues/387)) ([bbfc6c3](https://github.com/SenteLabsAI/OpenExecutive/commit/bbfc6c3d150ee3db51722e618d0a95925e8eded1))
* **delegation:** find and reopen the person's own emails across conversations ([#397](https://github.com/SenteLabsAI/OpenExecutive/issues/397)) ([49ed3cc](https://github.com/SenteLabsAI/OpenExecutive/commit/49ed3cccc07f2b1bc9cdc600b43fa97d3fa25cb3))
* **delegation:** leave approval cards for actions an email calls for ([#388](https://github.com/SenteLabsAI/OpenExecutive/issues/388)) ([44a1807](https://github.com/SenteLabsAI/OpenExecutive/commit/44a1807067ae8a0e3924639b6a947881fae2cbfe))
* **delegation:** lock down only the turn that read the mail ([#385](https://github.com/SenteLabsAI/OpenExecutive/issues/385)) ([a794917](https://github.com/SenteLabsAI/OpenExecutive/commit/a794917b85e94cbaa88582e955b8ba057712c918))
* **delegation:** put each Act as me setting in training on its own ([#408](https://github.com/SenteLabsAI/OpenExecutive/issues/408)) ([7ae0737](https://github.com/SenteLabsAI/OpenExecutive/commit/7ae0737f023845b29713a1f3d3db9bdf23dc77d7))
* **delegation:** read the speaker's own mailbox and attachments from chat ([#384](https://github.com/SenteLabsAI/OpenExecutive/issues/384)) ([5ae7674](https://github.com/SenteLabsAI/OpenExecutive/commit/5ae76741e0b9ffdb997130f6a10a45a1f3400b8f))
* **memory:** let each person share their work style with the team ([#391](https://github.com/SenteLabsAI/OpenExecutive/issues/391)) ([dee0275](https://github.com/SenteLabsAI/OpenExecutive/commit/dee02752d8f84448a9c3771613410a7397ce847d))
* **orchestrator:** add an In training mode to Take the lead ([#400](https://github.com/SenteLabsAI/OpenExecutive/issues/400)) ([57937ac](https://github.com/SenteLabsAI/OpenExecutive/commit/57937ac0a46beff20e8d698113458879934e60a3))
* **orchestrator:** let an installed extension add chat tools and a kind of document ([#410](https://github.com/SenteLabsAI/OpenExecutive/issues/410)) ([dc3c105](https://github.com/SenteLabsAI/OpenExecutive/commit/dc3c10539b7f63f47fc03c0ef570f0ff68f32755))
* **scripts:** let the Executive build and keep its own tools ([#383](https://github.com/SenteLabsAI/OpenExecutive/issues/383)) ([5e4348d](https://github.com/SenteLabsAI/OpenExecutive/commit/5e4348dc8731380e9302b3a2ba0083d5671eaf99))
* **workflows:** edit a saved workflow by conversation ([#380](https://github.com/SenteLabsAI/OpenExecutive/issues/380)) ([a4677ad](https://github.com/SenteLabsAI/OpenExecutive/commit/a4677addc3b8c784af8e28bd354d598f2da960d7))
* **workflows:** let Python jobs run on a separate runner ([#402](https://github.com/SenteLabsAI/OpenExecutive/issues/402)) ([d266972](https://github.com/SenteLabsAI/OpenExecutive/commit/d266972a9b4cf43ee354d74caa06a7b93fb2ba8b))
* **workflows:** run Python jobs on files and keep them as custom tools ([#386](https://github.com/SenteLabsAI/OpenExecutive/issues/386)) ([f7a759d](https://github.com/SenteLabsAI/OpenExecutive/commit/f7a759d6fa711924282dbf973fef5ccfe6eab0e3))


### Fixed

* **chat:** label a calendar delete through manage_event as a removal ([#395](https://github.com/SenteLabsAI/OpenExecutive/issues/395)) ([f060ee7](https://github.com/SenteLabsAI/OpenExecutive/commit/f060ee7bb2d003bdbaa1ad09fb18e152aed9d7f6))
* **delegation:** ask instead of block after reading mail ([#405](https://github.com/SenteLabsAI/OpenExecutive/issues/405)) ([4a2ff41](https://github.com/SenteLabsAI/OpenExecutive/commit/4a2ff419c77a1fbadf412f6eb8253351d35ecf15))
* **delegation:** let Drive, Docs and Sheets reads run after a mail read ([#396](https://github.com/SenteLabsAI/OpenExecutive/issues/396)) ([643287c](https://github.com/SenteLabsAI/OpenExecutive/commit/643287cdfb1e61c7d8bd6ea994be332a858ba593))
* **delegation:** lock only links, scripts and broadcasts after a mail read ([#401](https://github.com/SenteLabsAI/OpenExecutive/issues/401)) ([deb44bd](https://github.com/SenteLabsAI/OpenExecutive/commit/deb44bd2be252de9021a178d4c035aac12ff9a6f))
* **deps:** bake the tool-search model with the fastembed the gateway runs ([#392](https://github.com/SenteLabsAI/OpenExecutive/issues/392)) ([96d5e3a](https://github.com/SenteLabsAI/OpenExecutive/commit/96d5e3acd60e9e2c3b9b6c8d0de05a1628464b9a))
* **orchestrator:** gate send_slack_dm on the People roster ([#404](https://github.com/SenteLabsAI/OpenExecutive/issues/404)) ([14f785f](https://github.com/SenteLabsAI/OpenExecutive/commit/14f785f57d841e1f21b6ed7d5f3914f32add28f2))
* **orchestrator:** send a message only to the principal without an approval card ([#393](https://github.com/SenteLabsAI/OpenExecutive/issues/393)) ([417771c](https://github.com/SenteLabsAI/OpenExecutive/commit/417771c20641009cce7f3123dd5f88bd1c987749))
* **orchestrator:** withhold run_script from turns that are not the principal's ([#411](https://github.com/SenteLabsAI/OpenExecutive/issues/411)) ([f87f137](https://github.com/SenteLabsAI/OpenExecutive/commit/f87f1375ee5afdc05bffee8b96e4e61de2737efa))
* **workflows:** offer Python jobs without files and name a runner's extra libraries ([#409](https://github.com/SenteLabsAI/OpenExecutive/issues/409)) ([e290c37](https://github.com/SenteLabsAI/OpenExecutive/commit/e290c372fb1e359387c9b4236f5931aee6965124))


### Changed

* **orchestrator:** cache the conversation history between messages ([#389](https://github.com/SenteLabsAI/OpenExecutive/issues/389)) ([2f9560b](https://github.com/SenteLabsAI/OpenExecutive/commit/2f9560b21c28795f9323cebc9eb71662453ed5cc))
* **orchestrator:** offer less common tools on demand and trim tool results and specialist lookups ([#406](https://github.com/SenteLabsAI/OpenExecutive/issues/406)) ([303d45e](https://github.com/SenteLabsAI/OpenExecutive/commit/303d45eaa0b2323f2e9646d19bf35dbcc98c6d71))
* **orchestrator:** share one reading budget per turn and build a tool for multi-read answers ([#407](https://github.com/SenteLabsAI/OpenExecutive/issues/407)) ([1548742](https://github.com/SenteLabsAI/OpenExecutive/commit/15487421f6b498826b6c5d347f888c607da771dc))

## [0.5.2](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.5.1...v0.5.2) (2026-10-06)


### Added

* **delegation:** let people describe how they write in their own words ([#362](https://github.com/SenteLabsAI/OpenExecutive/issues/362)) ([593741e](https://github.com/SenteLabsAI/OpenExecutive/commit/593741ee2b24ba3ced6294683a8e09f1fd05909a))
* **onboarding:** describe your work first, then ask what's missing ([#377](https://github.com/SenteLabsAI/OpenExecutive/issues/377)) ([8e112dc](https://github.com/SenteLabsAI/OpenExecutive/commit/8e112dcf3e82901c78d9a3677a119217768ca51e))
* **providers:** default to Claude Sonnet 5.5 and Opus 5.5 ([#371](https://github.com/SenteLabsAI/OpenExecutive/issues/371)) ([12b89ce](https://github.com/SenteLabsAI/OpenExecutive/commit/12b89cec4aef884b88cebe9e8522c0731608224e))
* **ui:** move your own memory card to Settings as About you ([#359](https://github.com/SenteLabsAI/OpenExecutive/issues/359)) ([dad7384](https://github.com/SenteLabsAI/OpenExecutive/commit/dad7384202a14657f287b8c1dff8e16ba29f9582))
* **ui:** play the Council Consult animation while the Executive works ([#368](https://github.com/SenteLabsAI/OpenExecutive/issues/368)) ([639f968](https://github.com/SenteLabsAI/OpenExecutive/commit/639f968d2eaa784054202f38a1e74280996511e4))
* **ui:** reword the who-is-it-for setup choice as Personal and Team ([#375](https://github.com/SenteLabsAI/OpenExecutive/issues/375)) ([892e96b](https://github.com/SenteLabsAI/OpenExecutive/commit/892e96b07aad98a0cc448ee0d926c0ad80b03250))
* **workflows:** let the morning reflection search the knowledge base ([#364](https://github.com/SenteLabsAI/OpenExecutive/issues/364)) ([e7106ab](https://github.com/SenteLabsAI/OpenExecutive/commit/e7106ab6d298dd719c830e06247d9298db0f2176))


### Fixed

* **delegation:** let the email's own greeting decide who a group email asks ([#365](https://github.com/SenteLabsAI/OpenExecutive/issues/365)) ([8145855](https://github.com/SenteLabsAI/OpenExecutive/commit/814585594ab802e3f5c714d3a7c5af311cae832b))
* **integrations:** stop logging the start of each email body ([#367](https://github.com/SenteLabsAI/OpenExecutive/issues/367)) ([35a1e6c](https://github.com/SenteLabsAI/OpenExecutive/commit/35a1e6c6158d9f3033ca3970a4cc6b22eaaf9937))
* **onboarding:** keep going when a setup turn returns prose or a null hint ([#378](https://github.com/SenteLabsAI/OpenExecutive/issues/378)) ([fd3e7bb](https://github.com/SenteLabsAI/OpenExecutive/commit/fd3e7bb7dbd8f540cae0d112f9c22a8a1550d7b8))
* **people:** point roster emails at Home and name who a reply is about ([#366](https://github.com/SenteLabsAI/OpenExecutive/issues/366)) ([8c15b9d](https://github.com/SenteLabsAI/OpenExecutive/commit/8c15b9d8b7376671bed6997331dd626eee56415b))
* **ui:** hide the Always asks first switches while Take the lead is off ([#360](https://github.com/SenteLabsAI/OpenExecutive/issues/360)) ([f1988d6](https://github.com/SenteLabsAI/OpenExecutive/commit/f1988d657a34a9919c07afb17922ae6ad4f0dc50))
* **ui:** keep the watch list page from scrolling the whole screen on phones ([#363](https://github.com/SenteLabsAI/OpenExecutive/issues/363)) ([4cd1136](https://github.com/SenteLabsAI/OpenExecutive/commit/4cd1136e3672ac5e3f4de6fe8abc8017f7e16edb))
* **ui:** put Dismiss and Edit in Gmail beside Send on reply cards ([#358](https://github.com/SenteLabsAI/OpenExecutive/issues/358)) ([7a278fa](https://github.com/SenteLabsAI/OpenExecutive/commit/7a278fae8e022283f0eb3985b737afd0eb4e9799))

## [0.5.1](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.5.0...v0.5.1) (2026-10-04)


### Added

* **delegation:** send inbox replies on their own under Handle it for me ([#353](https://github.com/SenteLabsAI/OpenExecutive/issues/353)) ([23af959](https://github.com/SenteLabsAI/OpenExecutive/commit/23af959100f7d66b87c9caa54df3370907f4b546))
* **orchestrator:** let the Executive take the lead behind one approval gate ([#355](https://github.com/SenteLabsAI/OpenExecutive/issues/355)) ([f341051](https://github.com/SenteLabsAI/OpenExecutive/commit/f3410516d10c623eb301276cebf6193d8b9a5553))


### Fixed

* **ui:** harden the UI for phones, readability and plain wording ([#357](https://github.com/SenteLabsAI/OpenExecutive/issues/357)) ([d46eee5](https://github.com/SenteLabsAI/OpenExecutive/commit/d46eee56d274823ee90f238de463d812d6244a39))

## [0.5.0](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.6...v0.5.0) (2026-10-03)


### Added

* **chat:** fold messages sent mid-turn into the running answer ([#352](https://github.com/SenteLabsAI/OpenExecutive/issues/352)) ([bba990f](https://github.com/SenteLabsAI/OpenExecutive/commit/bba990f20dab7559967010e65b65630f1a35c684))
* **delegation:** check Act as me drafts against what the writer already told those people ([#350](https://github.com/SenteLabsAI/OpenExecutive/issues/350)) ([0543e92](https://github.com/SenteLabsAI/OpenExecutive/commit/0543e928543f456586dbed04b38b1280753dbbab))
* **memory:** bring Always in the loop notes into the owner's briefs and remind when due ([#348](https://github.com/SenteLabsAI/OpenExecutive/issues/348)) ([a5b399d](https://github.com/SenteLabsAI/OpenExecutive/commit/a5b399d1da2708e07299b80b99ad146db3731835))
* **memory:** keep notes of what people say in chat ([#346](https://github.com/SenteLabsAI/OpenExecutive/issues/346)) ([eebda07](https://github.com/SenteLabsAI/OpenExecutive/commit/eebda072821a3294c2ce09b9c3cd2f7372a44885))
* **memory:** keep private notes from replies you approve and send ([#342](https://github.com/SenteLabsAI/OpenExecutive/issues/342)) ([96098b7](https://github.com/SenteLabsAI/OpenExecutive/commit/96098b7d2a5898c5970a207582a8b982580ea329))
* **ui:** add the History tab and settings for Always in the loop ([#344](https://github.com/SenteLabsAI/OpenExecutive/issues/344)) ([870ff2b](https://github.com/SenteLabsAI/OpenExecutive/commit/870ff2b146b84faf12892eba132ae3b42b8e9e1d))
* **ui:** move the Agent Council's Advanced toggle into the agent panel ([#345](https://github.com/SenteLabsAI/OpenExecutive/issues/345)) ([29de6ca](https://github.com/SenteLabsAI/OpenExecutive/commit/29de6ca9eb869b49c42f611cc03e039e8f8f20ce))
* **ui:** simplify every screen with a six-place menu and focused pages ([3155921](https://github.com/SenteLabsAI/OpenExecutive/commit/3155921a77a77057115f3c5c40018d1997ce749a))


### Fixed

* **artifacts:** keep each document private to the person who made it ([#351](https://github.com/SenteLabsAI/OpenExecutive/issues/351)) ([cbaad6f](https://github.com/SenteLabsAI/OpenExecutive/commit/cbaad6fc8109bde49cce6b9644a51f6de9ccd2d9))
* **delegation:** stop drafting inbox replies to group emails that ask someone else ([#343](https://github.com/SenteLabsAI/OpenExecutive/issues/343)) ([5da0b29](https://github.com/SenteLabsAI/OpenExecutive/commit/5da0b29821e54bc2651d51280a14388636160ca6))


### Changed

* **ci:** run tests with temp files in RAM and parse the package once ([#339](https://github.com/SenteLabsAI/OpenExecutive/issues/339)) ([75e3163](https://github.com/SenteLabsAI/OpenExecutive/commit/75e31638496feed9f28f6258be7f8ea8089d667b))

## [0.4.6](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.5...v0.4.6) (2026-10-02)


### Added

* **auth:** add SSO sign-in through any OpenID Connect provider ([#338](https://github.com/SenteLabsAI/OpenExecutive/issues/338)) ([d338f1f](https://github.com/SenteLabsAI/OpenExecutive/commit/d338f1f9088fefca2d6cbe4e760bd1261b33813a))
* **delegation:** let Act as me use an Outlook mailbox ([#336](https://github.com/SenteLabsAI/OpenExecutive/issues/336)) ([6b7957e](https://github.com/SenteLabsAI/OpenExecutive/commit/6b7957eef63a6e113944a0601e2681efb91013f8))
* **integrations:** add a Microsoft 365 (Outlook) mail and calendar backend ([#326](https://github.com/SenteLabsAI/OpenExecutive/issues/326)) ([5ec59a4](https://github.com/SenteLabsAI/OpenExecutive/commit/5ec59a4ccc2a8291693fbd22ec683bcf55da16cd))
* **integrations:** sync OneDrive folders into knowledge and add OneDrive tools in chat ([#331](https://github.com/SenteLabsAI/OpenExecutive/issues/331)) ([29d3037](https://github.com/SenteLabsAI/OpenExecutive/commit/29d3037b328b969e833428bb9f6ac3269d5f7d95))
* **knowledge:** sync Confluence spaces into the knowledge base ([#324](https://github.com/SenteLabsAI/OpenExecutive/issues/324)) ([27b44bc](https://github.com/SenteLabsAI/OpenExecutive/commit/27b44bc7baa59e4bc8488e1d8fad1781611bcee9))
* **settings:** show the running version and when a newer release is out ([#323](https://github.com/SenteLabsAI/OpenExecutive/issues/323)) ([5d23f29](https://github.com/SenteLabsAI/OpenExecutive/commit/5d23f299dea5cb786bb5b402ef2ba416565f15b9))


### Fixed

* **ui:** make `npm run lint` real again, package-wide, and enforce it in CI ([#327](https://github.com/SenteLabsAI/OpenExecutive/issues/327)) ([398519f](https://github.com/SenteLabsAI/OpenExecutive/commit/398519f3c53194bf0abb677cf5716858672e3f5f))
* **ui:** make the web app readable and usable on phones ([#335](https://github.com/SenteLabsAI/OpenExecutive/issues/335)) ([3ff5f50](https://github.com/SenteLabsAI/OpenExecutive/commit/3ff5f50d3982e3b0b5a36d0129a6bbc1ebc39523))

## [0.4.5](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.4...v0.4.5) (2026-10-01)


### Added

* **agents:** add additional instructions to Agent Council agents ([#308](https://github.com/SenteLabsAI/OpenExecutive/issues/308)) ([4bc9567](https://github.com/SenteLabsAI/OpenExecutive/commit/4bc9567cd1391456b0d03e2bb636bcf9de07c65b))
* **agents:** add Fast, Balanced and Thorough quality presets to the Agent Council ([#309](https://github.com/SenteLabsAI/OpenExecutive/issues/309)) ([58ba2ec](https://github.com/SenteLabsAI/OpenExecutive/commit/58ba2ec105b9cdfd842487b952fc1210669dba9e))
* **agents:** offer Direct, Supportive and Analytical voices in setup and Settings ([#310](https://github.com/SenteLabsAI/OpenExecutive/issues/310)) ([c99d9d1](https://github.com/SenteLabsAI/OpenExecutive/commit/c99d9d1edcc23870ff2c20f108e73e425128fb2e))
* **agents:** open the Agent Council in a simple view ([#311](https://github.com/SenteLabsAI/OpenExecutive/issues/311)) ([4e45588](https://github.com/SenteLabsAI/OpenExecutive/commit/4e45588f4a70507ab3fb8412511d99a669cc1be7))
* **delegation:** draft replies to your inbox and send them on your tap ([#287](https://github.com/SenteLabsAI/OpenExecutive/issues/287)) ([0980b75](https://github.com/SenteLabsAI/OpenExecutive/commit/0980b75774d7b43bd31acccf38deb686703b51d7))
* **delegation:** let team members use Act as me, with their mail private to them ([#320](https://github.com/SenteLabsAI/OpenExecutive/issues/320)) ([4b370b0](https://github.com/SenteLabsAI/OpenExecutive/commit/4b370b0b8e6939c247d5a3541de86617408f7462))
* **knowledge:** put company documents first and list Drive and Notion files ([#306](https://github.com/SenteLabsAI/OpenExecutive/issues/306)) ([7f1acd8](https://github.com/SenteLabsAI/OpenExecutive/commit/7f1acd8dfe0e528d0f79e16551ee1fc34eee3a5c))


### Fixed

* **knowledge:** accept Excel and CSV uploads on the documents page ([#318](https://github.com/SenteLabsAI/OpenExecutive/issues/318)) ([6ae0e6b](https://github.com/SenteLabsAI/OpenExecutive/commit/6ae0e6b8bbca56ef6a3f60e490483598910cf4a4)), closes [#316](https://github.com/SenteLabsAI/OpenExecutive/issues/316)
* **knowledge:** start one sync per double click and report partial sync failures ([#307](https://github.com/SenteLabsAI/OpenExecutive/issues/307)) ([649cdd8](https://github.com/SenteLabsAI/OpenExecutive/commit/649cdd8a85960c76c054712a2773bc8ce64fc50e))
* **slack:** show senders when a thread message goes unanswered ([#319](https://github.com/SenteLabsAI/OpenExecutive/issues/319)) ([6293b6f](https://github.com/SenteLabsAI/OpenExecutive/commit/6293b6f5ed420a43574af67c1c9508182d77c4fe))
* **ui:** pass uploads up to the api's limit through the middleware ([#303](https://github.com/SenteLabsAI/OpenExecutive/issues/303)) ([0977d06](https://github.com/SenteLabsAI/OpenExecutive/commit/0977d06be19494fde2958c5a24bde647cd3c2fd5))


### Changed

* **knowledge:** parse uploaded documents in a short-lived child process ([#305](https://github.com/SenteLabsAI/OpenExecutive/issues/305)) ([cd8b3ec](https://github.com/SenteLabsAI/OpenExecutive/commit/cd8b3ec7209c95bcafbf83693c0e3154a4d892e4))

## [0.4.4](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.3...v0.4.4) (2026-09-30)


### Added

* **alerts:** let the principal find and clear alerts off the live board ([#299](https://github.com/SenteLabsAI/OpenExecutive/issues/299)) ([7bee3dc](https://github.com/SenteLabsAI/OpenExecutive/commit/7bee3dc4fa0e4a4c32e3704ccf3ee23e9264001f))
* **attunement:** let people assign a task to a teammate ([#285](https://github.com/SenteLabsAI/OpenExecutive/issues/285)) ([13da433](https://github.com/SenteLabsAI/OpenExecutive/commit/13da433bc6f3ae97e78bb8c90f06bb5e49953447))
* **knowledge:** add the files in chosen Google Drive folders to the knowledge base ([#279](https://github.com/SenteLabsAI/OpenExecutive/issues/279)) ([354ddb4](https://github.com/SenteLabsAI/OpenExecutive/commit/354ddb4c4f1ea66365e0530b583667a16447962d))
* **memory:** let teammates record attributed standing facts ([#280](https://github.com/SenteLabsAI/OpenExecutive/issues/280)) ([ede7b86](https://github.com/SenteLabsAI/OpenExecutive/commit/ede7b86a83275dd973dd7cd454ed0f11d2bf9247))


### Fixed

* **deps:** move the mcp gateway's dependency cutoff past fastembed 0.8.0 ([#288](https://github.com/SenteLabsAI/OpenExecutive/issues/288)) ([8098ed5](https://github.com/SenteLabsAI/OpenExecutive/commit/8098ed552e5b80c50ec3565f4e483921644504ac))
* **integrations:** acknowledge a new email sender only when Gmail authenticated them ([#283](https://github.com/SenteLabsAI/OpenExecutive/issues/283)) ([35e00d3](https://github.com/SenteLabsAI/OpenExecutive/commit/35e00d34771f85d036fc2c8c684d7a5fcffe886d))
* **integrations:** pin the mcp gateway to extensible-mcp with a leaner tool index ([#291](https://github.com/SenteLabsAI/OpenExecutive/issues/291)) ([1a49047](https://github.com/SenteLabsAI/OpenExecutive/commit/1a4904797ffc8c1aa4d947ba9c5e8bd74396f9d2))
* **integrations:** pin the mcp gateway to extensible-mcp with batched embedding ([#290](https://github.com/SenteLabsAI/OpenExecutive/issues/290)) ([5b342bc](https://github.com/SenteLabsAI/OpenExecutive/commit/5b342bc9bc0acff103075f2146511b03d8bc8bd3))
* **knowledge:** share one lean embedding session across chroma collections ([#292](https://github.com/SenteLabsAI/OpenExecutive/issues/292)) ([b05a75b](https://github.com/SenteLabsAI/OpenExecutive/commit/b05a75b5f43fb1f4b619fecb3550ecea9e4c9cfb))
* **mcp:** pin extensible-mcp to the commit that supports mcp 2.x ([#298](https://github.com/SenteLabsAI/OpenExecutive/issues/298)) ([d06bae2](https://github.com/SenteLabsAI/OpenExecutive/commit/d06bae2f4fae1ff16d0b2950d21b64336b89ac71))
* **memory:** keep strangers' email out of decisions under one untrusted-content policy ([#284](https://github.com/SenteLabsAI/OpenExecutive/issues/284)) ([966821a](https://github.com/SenteLabsAI/OpenExecutive/commit/966821aeff3e7162f99704f836f3a6f1aae17c73))

## [0.4.3](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.2...v0.4.3) (2026-09-29)


### Added

* **briefing:** feed the principal's live day into the header and the brief ([#263](https://github.com/SenteLabsAI/OpenExecutive/issues/263)) ([7fea2d5](https://github.com/SenteLabsAI/OpenExecutive/commit/7fea2d514c0d6dc4b567b1266a5275d5312318c4))
* **briefing:** ground names and figures in unattended prose before delivery ([#272](https://github.com/SenteLabsAI/OpenExecutive/issues/272)) ([a565c30](https://github.com/SenteLabsAI/OpenExecutive/commit/a565c30634c1a0d2413522f8d724e2283f47fb45))
* **memory:** keep chat corrections as standing facts in prompts ([#270](https://github.com/SenteLabsAI/OpenExecutive/issues/270)) ([4e2a42b](https://github.com/SenteLabsAI/OpenExecutive/commit/4e2a42b8816a3254c7bfbab58058a36e8e185b04))
* **memory:** remember the Drive files a conversation found or read ([#276](https://github.com/SenteLabsAI/OpenExecutive/issues/276)) ([f200cd9](https://github.com/SenteLabsAI/OpenExecutive/commit/f200cd99ceb571681cf4ad8e8319897883de77b5))
* **orchestrator:** tell the executive which systems are connected ([#271](https://github.com/SenteLabsAI/OpenExecutive/issues/271)) ([8c742b3](https://github.com/SenteLabsAI/OpenExecutive/commit/8c742b3c4c7362f1ca16127bd989556cb44d41e5))
* **people:** let the owner confirm and add unknown senders ([#273](https://github.com/SenteLabsAI/OpenExecutive/issues/273)) ([ff693ae](https://github.com/SenteLabsAI/OpenExecutive/commit/ff693ae50cc7a05ddf9d63ea2d57bc727dae82b8))
* **providers:** add LOCAL_REASONING_EFFORT for thinking-only models ([#274](https://github.com/SenteLabsAI/OpenExecutive/issues/274)) ([ed06025](https://github.com/SenteLabsAI/OpenExecutive/commit/ed06025aabf3a3c381a690f13192d0bd2e104904))


### Fixed

* **ui:** show turn progress while the executive is still working ([#261](https://github.com/SenteLabsAI/OpenExecutive/issues/261)) ([29f634b](https://github.com/SenteLabsAI/OpenExecutive/commit/29f634ba9d69b1c91d35230e0e08d3fd3622872c))
* update the Apache license file ([#275](https://github.com/SenteLabsAI/OpenExecutive/issues/275)) ([421db3c](https://github.com/SenteLabsAI/OpenExecutive/commit/421db3c402a652ae9e0693e922f3b4c96283e807))

## [0.4.2](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.1...v0.4.2) (2026-09-28)


### Added

* **delegation:** draft replies in your voice from your own Gmail ([#242](https://github.com/SenteLabsAI/OpenExecutive/issues/242)) ([cc2e95f](https://github.com/SenteLabsAI/OpenExecutive/commit/cc2e95f386d3ac1a65ed923a16cac6b2f2e70216))
* **knowledge:** read scanned PDFs on every channel ([#254](https://github.com/SenteLabsAI/OpenExecutive/issues/254)) ([a083f18](https://github.com/SenteLabsAI/OpenExecutive/commit/a083f1851492b529047c1b2ae2b30505a07ac170))
* **providers:** read PDFs through each provider's own file support ([#257](https://github.com/SenteLabsAI/OpenExecutive/issues/257)) ([9e0bbc4](https://github.com/SenteLabsAI/OpenExecutive/commit/9e0bbc4dea179a1342f239572081989c2c523817))
* **ui:** give the settings page a section nav and grouped tools ([#260](https://github.com/SenteLabsAI/OpenExecutive/issues/260)) ([f896562](https://github.com/SenteLabsAI/OpenExecutive/commit/f89656265e9f1bc84de7ed4967cb13ae44a020fc))
* **ui:** make goals quick to add with one required field ([#239](https://github.com/SenteLabsAI/OpenExecutive/issues/239)) ([5219d0a](https://github.com/SenteLabsAI/OpenExecutive/commit/5219d0a84d6691b5e08ff01a7b755961f868dd58))
* **ui:** open workflows on a start-here panel ([#236](https://github.com/SenteLabsAI/OpenExecutive/issues/236)) ([ab37898](https://github.com/SenteLabsAI/OpenExecutive/commit/ab37898471e1cb1d92ee26fd2a8c086899ead18c))


### Fixed

* **delegation:** show your whole writing profile and keep its line breaks ([#252](https://github.com/SenteLabsAI/OpenExecutive/issues/252)) ([3f6ba5b](https://github.com/SenteLabsAI/OpenExecutive/commit/3f6ba5bf7cffb881b15f0ef05524e045e28a17e0))
* **evals:** point make eval and docs at the packaged scenarios ([#240](https://github.com/SenteLabsAI/OpenExecutive/issues/240)) ([a0f31d9](https://github.com/SenteLabsAI/OpenExecutive/commit/a0f31d907f6c68bee27712ebdf56bbea8ea9c329))
* **integrations:** record the inbound email's text in its audit row ([#259](https://github.com/SenteLabsAI/OpenExecutive/issues/259)) ([8d1ca4b](https://github.com/SenteLabsAI/OpenExecutive/commit/8d1ca4b8b55362538bef4676d54d09340c1853bb))
* **orchestrator:** close the paths around the Google Workspace egress gates ([#258](https://github.com/SenteLabsAI/OpenExecutive/issues/258)) ([eb230cc](https://github.com/SenteLabsAI/OpenExecutive/commit/eb230cc759d1cd9eb84ac56a2a88060cb639ef4d))
* **ui:** anchor the auth middleware's path exclusions ([#251](https://github.com/SenteLabsAI/OpenExecutive/issues/251)) ([b90c611](https://github.com/SenteLabsAI/OpenExecutive/commit/b90c611d0889472a35469d81f81ed052cbef2fee))

## [0.4.1](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.4.0...v0.4.1) (2026-09-25)


### Added

* add local login for make dev and link the owner at setup ([#218](https://github.com/SenteLabsAI/OpenExecutive/issues/218)) ([a8efeed](https://github.com/SenteLabsAI/OpenExecutive/commit/a8efeeddf3f969a9c2439ce88f4aca9a644d050f))
* **agents:** add a sales specialist and advice for bootstrapped founders ([#220](https://github.com/SenteLabsAI/OpenExecutive/issues/220)) ([fd7a8aa](https://github.com/SenteLabsAI/OpenExecutive/commit/fd7a8aae60dbb8d68678cee86901fc9775368c97))
* **briefing:** email briefs as a backup and show when one isn't sent ([#226](https://github.com/SenteLabsAI/OpenExecutive/issues/226)) ([71a75c8](https://github.com/SenteLabsAI/OpenExecutive/commit/71a75c818f7bedc526a37c72fb951c77089c6146))
* **chat:** keep answers when an expert fails and show their sources ([#222](https://github.com/SenteLabsAI/OpenExecutive/issues/222)) ([90ca2b2](https://github.com/SenteLabsAI/OpenExecutive/commit/90ca2b2d3c708ef160a3b049c99d584e74add4c5))
* **core:** add workspace settings for your time zone and a solo mode ([#221](https://github.com/SenteLabsAI/OpenExecutive/issues/221)) ([345a91a](https://github.com/SenteLabsAI/OpenExecutive/commit/345a91a65f36c713280878337dee04a0e62f6867))
* **goals:** track your goals and dated commitments in solo mode ([#228](https://github.com/SenteLabsAI/OpenExecutive/issues/228)) ([242af54](https://github.com/SenteLabsAI/OpenExecutive/commit/242af54c771de5a593421ae8b79e91ce8fbf541e))
* **onboarding:** ask what your role is when you use Open Executive just for yourself ([#230](https://github.com/SenteLabsAI/OpenExecutive/issues/230)) ([5dc3a43](https://github.com/SenteLabsAI/OpenExecutive/commit/5dc3a438694000c9373008ca54757afbde602c7b))
* **orchestrator:** work for one person in solo mode ([#225](https://github.com/SenteLabsAI/OpenExecutive/issues/225)) ([1c35275](https://github.com/SenteLabsAI/OpenExecutive/commit/1c35275d306ae684edeba329ed422f737b4d5bc7))
* **people:** keep contacts apart from your team and act on mail you forward ([#233](https://github.com/SenteLabsAI/OpenExecutive/issues/233)) ([1bf703d](https://github.com/SenteLabsAI/OpenExecutive/commit/1bf703d17e191e093bff6c388628ff511bfeba5d))
* **ui:** add a setup status page that tests each part of the install ([#219](https://github.com/SenteLabsAI/OpenExecutive/issues/219)) ([a95f924](https://github.com/SenteLabsAI/OpenExecutive/commit/a95f924dd54813c708d279419f0654c6f5810e33))
* **ui:** describe Just me for any role, not only someone running a business ([#234](https://github.com/SenteLabsAI/OpenExecutive/issues/234)) ([7d97b8d](https://github.com/SenteLabsAI/OpenExecutive/commit/7d97b8d006f6fd01df2e3d64f194f110ca4c2040))
* **ui:** set up and run Open Executive just for yourself ([#224](https://github.com/SenteLabsAI/OpenExecutive/issues/224)) ([bf3670a](https://github.com/SenteLabsAI/OpenExecutive/commit/bf3670a882418ee1abe14bcfba0cee3559c0188f))
* **workflows:** add a weekly review and a daily top three ([#232](https://github.com/SenteLabsAI/OpenExecutive/issues/232)) ([62b283f](https://github.com/SenteLabsAI/OpenExecutive/commit/62b283f2bcd4399eef861e7df39ef486ef59a5cc))


### Fixed

* hold escalations, allow cancel, and keep roster edits owner-only ([#216](https://github.com/SenteLabsAI/OpenExecutive/issues/216)) ([4975dff](https://github.com/SenteLabsAI/OpenExecutive/commit/4975dfffc9bedadf0d94b0efaee4c18fcf2a6566))
* **people:** stop teammates taking over the owner's entry ([#227](https://github.com/SenteLabsAI/OpenExecutive/issues/227)) ([9325113](https://github.com/SenteLabsAI/OpenExecutive/commit/9325113ea395fa2d78a3015e4284e1ce3f2b0354))
* **scheduler:** run check-ins, chase idle initiatives and honour brief channels ([#223](https://github.com/SenteLabsAI/OpenExecutive/issues/223)) ([2a31596](https://github.com/SenteLabsAI/OpenExecutive/commit/2a3159653750702709f36f0d4e891cf3d1a95422))

## [0.4.0](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.3.2...v0.4.0) (2026-09-24)


### Added

* **artifacts:** deliver artifacts by message link or email attachment ([#209](https://github.com/SenteLabsAI/OpenExecutive/issues/209)) ([212cfaf](https://github.com/SenteLabsAI/OpenExecutive/commit/212cfafc2c2e7a6cc9cf734581d7dc65b05e59f8))
* **artifacts:** publish, reread and revise artifacts in html, docx, xlsx or links ([#207](https://github.com/SenteLabsAI/OpenExecutive/issues/207)) ([066abd3](https://github.com/SenteLabsAI/OpenExecutive/commit/066abd320928397cf1044ce7a5704de1fd5c4514))
* **knowledge:** make chat-proposed playbooks drafts a person approves ([#208](https://github.com/SenteLabsAI/OpenExecutive/issues/208)) ([b775dea](https://github.com/SenteLabsAI/OpenExecutive/commit/b775deac7231b779830d1939499edaead3caf689))
* **scheduler:** add a global pause for the executive's autonomous work ([812a39e](https://github.com/SenteLabsAI/OpenExecutive/commit/812a39ee220d639c33d7006c1be2c9e461cfa903))
* **ui:** add a vitals panel to the Pulse heartbeat card ([#203](https://github.com/SenteLabsAI/OpenExecutive/issues/203)) ([4101207](https://github.com/SenteLabsAI/OpenExecutive/commit/4101207b487cc5718ef451b0e330998cd9e0772f))
* **ui:** manage skills as playbooks on the Workflows page ([#202](https://github.com/SenteLabsAI/OpenExecutive/issues/202)) ([4691e48](https://github.com/SenteLabsAI/OpenExecutive/commit/4691e489c520323acd18a43a2c2722a0dff6d11d))
* **ui:** rename Artifacts to Documents for users and the Executive ([#214](https://github.com/SenteLabsAI/OpenExecutive/issues/214)) ([67ee80d](https://github.com/SenteLabsAI/OpenExecutive/commit/67ee80d6bdbecf8d36be1498b595317d8a2e83c6))
* **ui:** share one sidebar with a short recent-chats list on every page ([#195](https://github.com/SenteLabsAI/OpenExecutive/issues/195)) ([59b7d35](https://github.com/SenteLabsAI/OpenExecutive/commit/59b7d35813e26ac123f72be4fc1c72318a6f1d94))
* **ui:** show every peer-memory note in a scrollable People tab ([#201](https://github.com/SenteLabsAI/OpenExecutive/issues/201)) ([daabb99](https://github.com/SenteLabsAI/OpenExecutive/commit/daabb99c5ea92fefd9dfe5e9eda813f12b657d77))
* **workflows:** confirm a workflow's first write to a new target ([#199](https://github.com/SenteLabsAI/OpenExecutive/issues/199)) ([d63d11d](https://github.com/SenteLabsAI/OpenExecutive/commit/d63d11dcb570750ee43f50628324c8579597f197))
* **workflows:** have workflows follow playbooks ([#206](https://github.com/SenteLabsAI/OpenExecutive/issues/206)) ([1c61e18](https://github.com/SenteLabsAI/OpenExecutive/commit/1c61e187005c9dc5d43bf0160e9334de88b00e64))
* **workflows:** let workflow steps use tools ([#193](https://github.com/SenteLabsAI/OpenExecutive/issues/193)) ([429aeda](https://github.com/SenteLabsAI/OpenExecutive/commit/429aeda507373e3536313c25f2793f7983740900))


### Fixed

* **api:** check session ownership on every per-session route ([#197](https://github.com/SenteLabsAI/OpenExecutive/issues/197)) ([cf61609](https://github.com/SenteLabsAI/OpenExecutive/commit/cf61609ff8a3bb44879e365c232f24ab3a254b83))
* **api:** remove the unauthenticated /debug/last-turn endpoint ([#200](https://github.com/SenteLabsAI/OpenExecutive/issues/200)) ([112486d](https://github.com/SenteLabsAI/OpenExecutive/commit/112486dfc6d560e3695d575df6f1a50ad1c894fa))
* **ui:** call the Jobs page "Workflows" ([#194](https://github.com/SenteLabsAI/OpenExecutive/issues/194)) ([d3e48cb](https://github.com/SenteLabsAI/OpenExecutive/commit/d3e48cbc9558f6eab97d07cc1145ff317918adec))
* **ui:** compare complete days only in the heartbeat trend ([#204](https://github.com/SenteLabsAI/OpenExecutive/issues/204)) ([f4a68b6](https://github.com/SenteLabsAI/OpenExecutive/commit/f4a68b60092e08365ad267c11d2fdf135ebd6b34))


### Changed

* **core:** parallelize unit tests with pytest-xdist ([#213](https://github.com/SenteLabsAI/OpenExecutive/issues/213)) ([8b9a822](https://github.com/SenteLabsAI/OpenExecutive/commit/8b9a82289ccba4feadf9b09b62597e3be15b0e1f))


### Documentation

* **release:** document forcing a version with Release-As ([e71a025](https://github.com/SenteLabsAI/OpenExecutive/commit/e71a025af7bc8ea4848a8c428da4b4570f4498e1))

## [0.3.2](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.3.1...v0.3.2) (2026-09-23)


### Added

* **ui:** add an assistant-led workflow wizard and compact the jobs catalog ([#189](https://github.com/SenteLabsAI/OpenExecutive/issues/189)) ([c52b96c](https://github.com/SenteLabsAI/OpenExecutive/commit/c52b96c551143246e15fc938806f9fe9b6975de9))


### Fixed

* **ui:** validate check-in cadences and clear emptied leave dates ([#191](https://github.com/SenteLabsAI/OpenExecutive/issues/191)) ([5940557](https://github.com/SenteLabsAI/OpenExecutive/commit/594055792c346622fb174d65164c67cc77e346bd))

## [0.3.1](https://github.com/SenteLabsAI/OpenExecutive/compare/v0.3.0...v0.3.1) (2026-09-23)


### Added

* **chat:** suggest a follow-up in the composer ([#182](https://github.com/SenteLabsAI/OpenExecutive/issues/182)) ([7f7c1c1](https://github.com/SenteLabsAI/OpenExecutive/commit/7f7c1c1e2304c7333f83282fdcfb544f54fb47f0))
* **ui:** group council model picker by provider ([#186](https://github.com/SenteLabsAI/OpenExecutive/issues/186)) ([8b3fb2f](https://github.com/SenteLabsAI/OpenExecutive/commit/8b3fb2fdf531eab1687443b99aa7f439c5dfd16f))


### Fixed

* **memory:** quote extraction and open loops from the speaker's own words ([#187](https://github.com/SenteLabsAI/OpenExecutive/issues/187)) ([100f7d9](https://github.com/SenteLabsAI/OpenExecutive/commit/100f7d9ba07de8ce06a8a0434815f173a39271ee))
* **memory:** record only the person's own words in peer memory ([#185](https://github.com/SenteLabsAI/OpenExecutive/issues/185)) ([c32cf49](https://github.com/SenteLabsAI/OpenExecutive/commit/c32cf49c43137d20b325a29e2f576e1cda6e7fa9))

## [0.3.0] - 2026-09-23

### Added
- **Attunement: the Executive follows up on what people owe** (#175). When
  anyone on the roster commits to something in chat ("I'll send the vendor
  quote Thursday"), asks for something, or the principal says a teammate will
  do something, it becomes an open loop. Loops come due, get chased by the
  nudge engine through the usual routing and outbound checks, and close when
  their owner says it's done. The Executive can list them ("what is Sara
  waiting on?") and close one on request. Each person's page lists their open
  loops with Mark done. Every chat message now records who actually sent it,
  so nothing an outsider writes is read as someone on the roster.
- **👍/👎 on replies** (#175), in the main chat and the Ask OE panel.
- **Attunement: learning which proactive messages land** (#176). Every
  proactive DM (nudges, follow-ups, reflection, research and alert-review
  messages) is resolved as replied, acted on, ignored after 72 hours, or void
  when it stopped mattering. Credit only goes to the person who acted. A kind
  of nudge a person's last five resolved sends all went unanswered on ranks
  last for them on a longer cooldown until they answer one. The morning
  reflection gets a "What lands" summary, and each person's page a "How they
  respond" card.
- **Attunement: per-person working style** (#177). Up to four short rules on
  how to write replies for each person ("lead with the recommendation, then
  the numbers"), learned only from their own messages and 👍/👎 and pinned
  into their own conversations. Rules are checked before they are stored and
  every time they are used, and must be about how replies read, never an
  action. A "How I work with them" card lets the principal or the person
  edit, lock or reset them.
- New settings, all with defaults: `ATTUNEMENT_*` in `.env.example`.
  `ATTUNEMENT_ENABLED=false` turns off open-loop tracking and style learning;
  the outcome ledger has no switch and always records. None of it needs
  Honcho.

## [0.2.2] - 2026-09-22

### Changed
- **The MCP gateway runs a pinned extensible-mcp commit.** It was launched from
  the repo's default branch, so every container start ran whatever that
  branch held that day: a versioned image did not pin its gateway, rolling
  back an image did not roll the gateway back, and a start without network
  failed. The gateway and the image's pre-warm now launch the same commit
  with the same `--exclude-newer` cutoff, which also freezes extensible-mcp's
  own dependencies (uvx re-resolved those against PyPI on every start), and a
  unit test keeps the two in step. An image whose pre-warm succeeded now
  starts the gateway without network; the pre-warm is still best-effort, so
  a build during a GitHub outage ships an image that fetches it at first
  start. Updating the gateway is a deliberate bump of the commit and cutoff.

### Fixed
- **The compose health check no longer fails on a healthy API.** It runs
  `curl -f http://localhost:8000/health` inside the API container, but the
  image never installed `curl`, so the check failed on every run and compose
  reported a working API as unhealthy. The API image now includes `curl`.
- **The People tab shows the newest notes about a person** (#174). Its
  "recent" list and "learned" date asked Honcho for conclusions with
  `reverse=True`, which returns the oldest page, so a person's correction
  made in chat never appeared there. It now reads the newest page.

## [0.2.1] - 2026-09-22

### Changed
- **The API image installs the CPU-only build of torch.** torch is only
  present because `sentence-transformers` needs it, and the container runs on
  CPU hosts, but the default Linux wheel is the CUDA build and pulled in
  nineteen packages the image never used — fifteen `nvidia-*` libraries,
  three `cuda-*` shims and triton, about 2.2 GB of compressed wheels. torch
  now resolves from PyTorch's CPU index (2.13.0 → 2.14.0+cpu on Linux, plain
  2.14.0 on macOS, both from that index) and the Dockerfile installs from the
  lock with `uv sync` instead of an exported requirements file, so each
  package comes from the index the lock names. Expect the API image to shrink
  by several GB and cold builds and pulls to get much faster. `uv sync` gets
  the same wheels locally; a GPU deployment would need to override the index,
  and a plain `pip install` of the package (which ignores uv sources) still
  gets the CUDA build.

## [0.2.0] - 2026-09-22

### Security
- **The sign-in allow-list is now the union of `ALLOWED_EMAILS` and the People
  roster, not the roster alone** (#132). The UI previously treated the roster
  as authoritative as soon as it held one email, so any Person row with an
  address silently disabled `ALLOWED_EMAILS` — a fixture load, which wipes
  `people` and inserts its own addresses, could lock the configured operator
  out of their own instance. **This widens access on upgrade:** every address
  still sitting in `ALLOWED_EMAILS` regains sign-in even if that person is not
  on the roster, so audit the env var before deploying. Removing someone now
  means removing them from *both* the roster and `ALLOWED_EMAILS`. The
  `authorized` callback's fail-open also narrows: it used to admit any valid
  session whenever the roster fetch failed, and now admits only a session that
  is not in `ALLOWED_EMAILS` and whose roster membership is unreadable.
- `packages/ui`: force `lodash-es` to 4.18.1 via an npm `overrides` entry
  (GHSA-r5fr-rjxr-66jc code injection in `_.template`, GHSA-f23m-r3pf-42rh
  prototype pollution in `_.unset`/`_.omit`). The vulnerable 4.17.23 was pinned
  exactly by `chevrotain@11.1.2` underneath `mermaid@12.0.0`, and no mermaid
  release moves off it.

### Removed
- **Talent / executive search.** The whole vertical is gone: the `talent`
  specialist, `openexecutive/talent/` (engagements, candidates, offers, the
  ChromaDB matching graph), its 21 REST routes, 12 chat tools, 4 MCP tools and
  the `/talent` UI, plus the five recruiting workflows (`candidate_screen`,
  `candidate_outreach`, `interview_coordination`, `reference_check`,
  `offer_approval`). The `chro` specialist and its `comp_refresh` /
  `org_design` / `performance_review` / `exec_search_brief` workflows are
  unchanged — `exec_search_brief` is an advisory hiring brief, not pipeline.
- **Staff onboarding.** `openexecutive/staff_onboarding/` (templates, per-hire
  plans, tasks), its REST routes and 8 chat tools, the `new_hire_onboarding` and
  `role_onboarding` workflows, the `/staff-onboarding` UI, and the
  `onboarding_ramp` / `onboarding_kickoff` / `onboarding_checkin` scheduler
  kinds. **The company-setup wizard (`/onboard`) is untouched** — it is a
  different subsystem that happens to share the word.
- `GET /today` no longer returns the `talent` or `onboarding` fields, and the
  per-turn chat briefing no longer includes their digests.

  Existing SQLite tables (`engagements`, `candidates`, `offers`,
  `onboarding_templates`, `onboarding_plans`, `onboarding_tasks`) are **not
  dropped** — nothing reads them, and a blank client slot still wipes them so
  candidate data cannot cross slots. Pending talent reminders the removed
  workflows had scheduled on the principal's DM channel are cancelled by a
  one-time startup sweep (`cancel_orphaned_talent_reminders`, recorded in the
  new `app_migrations` table); the sweep can be deleted in the release after
  next.

### Changed
- **The chat turn ceiling is now 300s (360s with Committee review)**, up from
  120s/180s: deep multi-specialist turns were being cut off mid-answer. A
  ceiling this generous is only reasonable because a turn can now be ended by
  the user, so the two changes ship together. The onboarding interview, which
  previously borrowed `CHAT_STREAM_TIMEOUT_S`, gets its own
  `INTERVIEW_TIMEOUT_S` (still 120s) — it retries twice, so inheriting the new
  ceiling would have meant a 10-minute hang before the wizard surfaced a
  timeout.

### Fixed
- **A turn broken off early is no longer missing from the next turn's
  context.** On the disconnect and timeout paths the route persisted the
  partial turn to SQLite, but the Executive's post-turn block — which mirrors
  it into the live in-memory session — was skipped, and a cached session never
  re-reads its history from the DB. The next turn in the same process then
  prompted as though the turn had never happened, while a page reload showed
  it. The route now mirrors the turn itself on every broken-out path.
- **The UI proxy now forwards client disconnects upstream.** `signal:
  req.signal` was missing from the backend proxy's `fetch`, so the API never
  saw `http.disconnect` and its `request.is_disconnected()` check could not
  fire in production: closing a tab left the turn running to completion against
  Anthropic and the partial reply was never saved.
- **A disconnected turn no longer keeps working after the client is gone.**
  The SSE driver races the stop switch with `asyncio.wait`, which — unlike the
  `asyncio.wait_for` it replaced — does not cancel its futures when the task
  awaiting it is cancelled. Since Starlette cancels the response body on
  `http.disconnect`, the in-flight step is now cancelled in a `finally`, so a
  closed tab cannot leave a specialist or tool round running with no deadline
  and no persistence.

### Added
- **Versioned container images on GitHub Container Registry** (#142). Every
  push to `main` publishes `ghcr.io/sentelabsai/openexecutive-api:main` and
  `…/openexecutive-ui:main`; pushing a `vX.Y.Z` git tag publishes `X.Y.Z`,
  `X.Y` and `latest`. Deployments can pull a pinned version instead of
  building from source. See `docs/deployment.md` → Images.
- **Stop button in chat.** A reply can now be halted mid-stream, from the main
  chat composer and the Ask OE side panel (Escape works too). Whatever the
  Executive had written is kept, persisted and marked *Stopped by you*, so a
  truncated answer is not read back as a complete one after a reload. The
  client mints a `client_turn_id` and sends it with the turn; `POST /chat/stop`
  halts it. Keying on a client-minted id is what makes the button live from the
  moment Send is pressed — the server spends several seconds fetching context
  before the response stream exists, and a stop inside that window costs
  nothing because no model call has been made yet. An unknown id and another
  caller's turn both return 404, so the endpoint cannot be used to probe which
  turns are live.
- **Conversational onboarding.** `/onboard` now opens with "tell me about your
  company" instead of a 12-step form. The user writes a paragraph (and can
  attach a deck, one-pager or brief), the new `onboarding_interviewer` agent
  asks up to 8 clarifying questions, then drafts a company profile, leadership
  roster and department list that the user edits inline and saves. The draft
  review reuses the `/company-profile` section editors, extracted to
  `components/company-profile/ProfileSections.tsx`, so the editing surface is
  the same one the user gets permanently afterwards. New endpoints:
  `POST /onboard/interview/{start,message,draft,commit}` and
  `GET /onboard/interview/{session_id}`.

  Design notes: the interview never writes — `POST /onboard/interview/commit`
  is the single write, ordered so a rejected save leaves the draft editable
  and retryable. Departments are reconciled **additively** (matched ones
  updated, new ones created, none ever deleted), unlike the fixture loader
  which wipes the table and would destroy the eight defaults' `specialist_key`
  wiring. Exactly one person is marked principal and gets `WILDCARD`
  authority; no contact details are imported. Every failure path is asserted
  not to echo the user's input, because these transcripts carry ARR, burn and
  runway. The step-by-step wizard remains at `/onboard?mode=form` and still
  backs the `openexecutive onboard` CLI.
- **Research watch policy grounds in departments and recent decisions, and
  routes to department heads.** Departments gain a `watched_entities` list
  (`PATCH /departments/{slug}`, edited one per line on the department page).
  Named there, an entity is strong grounding for the research watch policy —
  like a profile vendor or ticker — so a proposal about it can be added on
  its own, and the watch is inserted with `route_to_department` /
  `route_to_person_id` (the head). Those columns now actually route: every
  watch alert carries the department's head and a `department:<slug>` tag,
  so it queues on the head's briefing instead of the principal's. Charter
  scope phrases and goal key results ground suggestions only. Suggestions in
  a department's area go to its head as one "Watch suggestions for
  <Department>" card per run (refreshed in place, re-issued weekly after it
  is handled; the principal when the department has no head), and the
  principal's pile-up nudge counts only their own. The ten most recent
  episodic decisions from the last 90 days are rendered to the research
  council and add a point to a proposal whose entity they name (with the
  decision's department as a routing hint). The research fingerprint tracks
  department watch interests and the decisions that name a known entity, so
  a new watched entity or a relevant decision triggers the next scan.
  `/watchlist` shows "for: <department>" on routed rows.
- **Watch proposals are linked to their evidence, and profile entries ground
  by name.** `propose_watch` now requires `finding_index`, and when the model
  omits it the policy links the finding that cites the source itself instead
  of rejecting the proposal for lack of evidence. Profile competitor / vendor
  / ticker entries and department watched entities are parsed to their names
  ("Tesla (TSLA) — Model Y…" grounds as Tesla plus the ticker TSLA; "GM /
  Chevrolet (…)" as both), so short names such as BYD, GM or Kia match and
  the description text can no longer stand in for the entity.
- **Research watchlist policy — grounded watches go straight in, uncertain
  ones become suggestions.** The research council's watchlist pass no longer
  adds watches itself; its only tool is `propose_watch`, and deterministic
  policy (`monitoring.research.watch_policy`) decides from company data. A
  proposal tied to a named competitor, vendor, ticker, initiative or
  priority (the profile gains `vendors` and `tickers` for this) and
  corroborated (own source, high-confidence finding, consensus,
  the policy's own track record) is added on its own — quietly: daily
  cadence, medium severity floor, a keyword trigger for feeds, 5 % for
  stocks — and shows up in the brief's "handled overnight" block as
  *watching*. Anything the Executive is not sure about lands as a dry-run
  **suggestion** (polls, never alerts) in a "Suggested by the Executive"
  section on `/watchlist` with Approve / Decline (reason: not relevant, too
  noisy, wrong source); the morning brief mentions the pending count in one
  line, and a single nudge alert fires only once ≥3 suggestions have waited
  ≥7 days. Declines are remembered by target and enforced in the tool
  handler, so a declined source is never re-proposed under a new slug;
  "Stop watching…" on a research-added card, `remove_watchlist_entry` and
  `DELETE /watchlist/{slug}?reason=` on research rows record one too.
  Approvals, declines, expiries and auto-disables feed the policy's history
  and the research turn ("stock watches grounded in a competitor: 4
  approved / 1 declined"). Research-added watches retire themselves: the
  scheduler sweep disables one that fired ≥5 alerts with ≥2 dismissed and
  low trust, or one with 3 consecutive poll failures (audited; re-enable
  from `/watchlist`; silence alone never retires a watch).
  Endpoints: `POST /watchlist/{slug}/approve`, `POST /watchlist/{slug}/decline`.
  Settings: `WATCHLIST_RESEARCH_MAX_DIRECT_ADDS=2`,
  `WATCHLIST_RESEARCH_MAX_PROPOSALS=2`, `WATCHLIST_MAX_ENABLED=40`,
  `WATCHLIST_PROPOSAL_TTL_DAYS=14`.
- **Self-maintaining alert feed.** Alerts get a per-category time-to-live
  (`ALERT_TTL_DAYS_ACTION=14`, `ALERT_TTL_DAYS_MONITORING=3`); a scheduler
  sweep expires past-TTL rows (audited, reversible via
  `POST /alerts/{id}/reopen`) and every surface reads the same live view.
  A repeat of an open alert with the same `(source, dedup_key)` now
  coalesces into the existing card (`occurrence_count`, `last_seen_at`)
  instead of stacking, and only re-pings when severity rose to high/urgent;
  monitoring alerts carry a stable `watch:<slug>` key so one watch means
  one card. A distinct `resolved` status keeps Executive closes separate
  from `ack` (user approved).
- **Executive alert review** (`alert_review_scan`, every 6 h and right before
  the morning brief): re-examines each open alert with evidence — newer
  signals from the same watch, related alerts, activity since, the roster
  with SLAs, department authority — and, through deterministic policy,
  routes + DMs the owner (`propose_via_alert` for propose-only departments),
  nudges, escalates to the principal with a deadline, drafts an artifact,
  suggests a workflow, folds duplicates, or resolves with evidence (high
  confidence only, citing a server-minted evidence ref — free text never
  closes a card; otherwise annotates "likely stale"). Alert text is rendered
  as inert data inside the review prompt (injection boundary); board / comp /
  legal matters only ever go to a scope-holder or the principal and keep
  their text; route/escalate are idempotent across passes; DMs carry an
  "[Alert review] Re: …" header. Capped per pass
  (`ALERT_REVIEW_MAX_MOVES_PER_SCAN`), single-flight, off for every caller
  with `ALERT_REVIEW_ENABLED=false`, every move audited with its evidence and
  prior state, every close reversible. `POST /alerts/review` runs it on
  demand.
- Briefing cards read as decisions: what changed since you last looked, the
  recommended move as the primary control, why-now / due chips, provenance
  (age, seen ×N), "N folded in", a "Handled overnight" rail with Undo, an
  "Executive handled N overnight" pill, a capped "Needs you" list with
  Show more, "Dismiss older than 7 days" (explicit ids), "Re-check
  relevance", and "Mute this topic" on dismiss.
- `POST /alerts/bulk-ack`, `POST /alerts/{id}/reopen`, `POST /alerts/review`.
- Dismissing a watch-sourced alert lowers that watch's `trust_score` (and
  bumps `dismiss_count`); approving recovers it. Trust now discounts the
  ranking and is shown to the review as evidence.

### Changed
- **"Handled overnight" now says what the Executive actually did, and says
  it with some pride.** The briefing rail no longer lists the review's
  `changed` verdict (an "Updated '<old headline>' — …" row): that rewrite
  leaves the alert open in "Needs you", where the card already shows the
  note, so the row double-reported it and called it done. Rewritten items
  now render in the morning brief and end-of-day digest as a REWRITTEN block
  under "what changed" (`brief_state.rewritten_since`), and a rewrite alone
  still un-suppresses the brief. Each remaining `/today.handled_overnight`
  row is structured (headline, target, detail, outcome, evidence_ref,
  event_type, and the alert's `status` now) instead of one truncated
  sentence; routed / nudged audit summaries name the person rather than
  "person 12", and the nudge bookkeeping marker is stripped for display.
  The rail is now one line by default — a count-only first-person heading
  ("Since your last brief: N off your plate, N in others' hands, N waiting
  on you", plus drafts / suggestions / watch changes / other moves / back
  on your plate when present) — with the rows behind a "details"
  disclosure, and nothing at all on a night with nothing handled. The
  grouping and summary logic lives in `packages/ui/src/lib/handled.ts`
  and is covered by `npm test`. Expanded, rows read first-person
  ("Resolved *X* — evidence", "Dismissed *X* as stale — …", "Handed *X* to
  Dana Kim", "Chased Dana Kim on *X*"), escalations first, one row per
  alert, every row linking its evidence to a pre-filtered `/audit` (the
  audit page now reads `event_type` / `q` from the query string) and a
  still-open row jumping to its card. Undo is only offered while the close
  still stands; a close already undone shows "Reopened" after a reload.
- **Specialists are told what departments watch.** The research context
  every specialist receives now carries a `DEPARTMENT WATCH INTERESTS`
  block (each department's watched entities), and the grounding rule
  admits a department interest or a company named in a recent decision.
  A Finance head who lists Brex now gets Brex findings; before, the
  specialists were never told and their grounding rule dropped them. The
  per-specialist finding cap is enforced in the tool schema and the
  parser, not only in prompt text.
- **The research run has its own controls.** `RESEARCH_WEB_SEARCH_MAX_USES`
  (default 3) caps searches per specialist independently of the chat knob;
  `RESEARCH_SPECIALISTS` picks which of the seven specialists run; the
  routing pass can no longer start a workflow (`run_workflow` is withheld,
  `suggest_workflow` remains); the periodic run's default interval moves
  from 120 to 360 minutes.
- **Web search reaches every OpenRouter model.** The provider feature gate
  stripped the web-search tool for non-Claude models, so a research
  specialist or the Executive pinned to Gemini, GPT, Llama, DeepSeek or
  Grok ran without search (and the research prompt then told it to emit
  nothing). The tool is now kept for any OpenRouter model and replaced by
  OpenRouter's own `openrouter:web_search` server tool (the deprecated web
  plugin is no longer used); only a self-hosted OpenAI-compatible backend
  still strips it. The server tool carries the same contract as
  Anthropic's: the model decides when to search, `max_uses` caps searches
  server-side, the domain allow/block lists apply, and the search count is
  reported, so `RESEARCH_WEB_SEARCH_MAX_USES` and `WEB_SEARCH_MAX_USES`
  mean the same thing on both deployment types.
- **Per-run watch budgets go to the strongest proposals.** The policy
  classifies every proposal first and applies them by tier (direct adds
  before suggestions) and descending score, so when the model files more
  candidates than the budgets or the ceiling allow, a better-grounded
  proposal filed later no longer loses its slot to weaker ones filed
  earlier, and a duplicate target keeps its strongest filing.
- **Every model call is recorded.** Research specialists, the research
  routing and watchlist passes, triage and the chat memory extractor now
  write the same `cache_event` audit row the Executive's chat turns always
  did (tokens, cache hits, cost, and the server-side web searches the call
  made). A research run's result and its `watchlist_research_periodic_ran`
  audit row carry a `usage` block summing its calls per source, and
  `GET /audit/usage` (and the usage page) gain a by-source breakdown.

### Removed
- **The xcrawl scrape service and everything that depended on it.** The
  research run no longer has a post-dedup verify pass or an optional
  read-before-cite scrape loop, and the watch policy no longer scores a
  "verified" point; the `XCRAWL_*`, `EXTERNAL_RESEARCH_VERIFY_*`,
  `RESEARCH_AGENTIC_*`, `RESEARCH_SCRAPE_*` and `RESEARCH_LOOP_*` settings
  are gone (a deployment that still sets them is unaffected). Every
  watchlist fetch is the keyless, SSRF-guarded httpx fetcher: a page watch
  written under the old `fetch: xcrawl` marker re-captures its baseline on
  the next poll without reporting a change, and an `rss` target that is not
  a feed becomes a `page_watch` when the page already fetched by the feed
  check has readable text (otherwise it is rejected, as before).

### Changed
- The research routing pass no longer carries the watchlist write tools
  (`add_watchlist_entry`, `tune_watchlist_entry`, `remove_watchlist_entry`):
  every watch a research run creates goes through the watchlist policy.
  `tune_watchlist_entry` refuses `mode` / `enabled` changes on a pending
  suggestion, URL targets are SSRF-checked at insert time for every kind,
  and deleting a watch now removes its signal history in the same
  transaction (the foreign key previously made any polled watch
  undeletable).
- Watchlist rows carry an `origin` (`manual` | `executive` | `research` |
  `research_proposed`); the research skip-if-unchanged fingerprint hashes
  only enabled, active rows so approving or declining a suggestion never
  triggers a fresh 7-specialist run; the research turn shows each watch's
  origin, fired/dismissed counts and trust, plus declined targets.
- Daily briefs are bounded to "since the last delivered brief": what's new,
  what the Executive handled overnight, who we're waiting on, one top call,
  and a one-line carried-over count. An unchanged day sends
  "Nothing new since yesterday's brief — N items still waiting on you." and
  skips the model call (`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED`; `force_full`
  on a manual run bypasses it). The EoD digest now excludes monitoring noise.
- The daily reflection sees yesterday's standup and is told not to repeat
  it; the watchlist research council's forced re-run moves from daily to
  weekly (`WATCHLIST_RESEARCH_MAX_STALENESS_HOURS=168`); stalled-workflow
  nudges stop after `NUDGE_MAX_PER_SCOPE=3` per item.
- Alerts created by the Executive's `create_alert` tool now land with their
  `routed_to_person_id` and `department:<slug>` tag (previously every
  triage-born alert fell to the principal as "unrouted").

### Upgrading
- All schema changes are additive columns with defaults (`alerts` table;
  `watchlist.origin`, plus two new tables `watchlist_declines` and
  `watchlist_policy_outcomes`); rolling the image back is safe. No config
  changes are required.
- The research council now adds a watch on its own only when it is grounded
  in your company data. Name your vendors and tickers on the Company
  Profile page ("External Dependencies") so status pages and filings for
  them land without asking; everything else arrives as a suggestion on
  `/watchlist`. Existing watches are untouched (`origin=manual`) and never
  auto-disabled.
- After upgrading, the first scheduler sweep expires alerts past their TTL
  (audited as `alert_sweep`, reversible with `reopen`); raise
  `ALERT_TTL_DAYS_*` (or set `0`) beforehand to keep an old backlog. The
  review job starts acting within `ALERT_REVIEW_MIN_AGE_HOURS` (2 h) — set
  `ALERT_REVIEW_ENABLED=false` to turn it off, or
  `ALERT_REVIEW_MAX_MOVES_PER_SCAN=0` to keep it annotate-only.
- Briefs get shorter and send a one-liner on unchanged days
  (`PRINCIPAL_BRIEF_SUPPRESS_UNCHANGED=false` restores daily full briefs).
- `docker/docker-compose.yml` now binds the API to `127.0.0.1:8000` instead of
  every host interface. The UI reaches the API over the compose network, so
  nothing in the stack needed the public port. If you were calling `:8000`
  directly from another host, put a reverse proxy in front of it and set
  `BACKEND_SHARED_SECRET` and `OE_PUBLIC_DEPLOYMENT=1`.

## [0.1.0] - 2026-06-30

Initial public release.

### Added
- Multi-agent "Executive" system: a single coherent executive persona backed by
  eight specialist sub-agents, powered by the Anthropic Claude API.
- Python backend (`packages/core`) — FastAPI service, orchestrator, specialist
  agents, ChromaDB-backed RAG, CLI, and prompt-caching layer.
- Next.js 15 web UI (`packages/ui`), including the static `/architecture` page.
- Curated MBA knowledge base (`knowledge/`) and eval suite (`evals/`).
- Optional integrations: Slack, Discord, and email.
- Docker deployment configuration.
- Open-source project setup: Apache-2.0 license, contribution guide, code of
  conduct, security policy, issue/PR templates, and CI.

[0.3.0]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/SenteLabsAI/OpenExecutive/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.1.0
