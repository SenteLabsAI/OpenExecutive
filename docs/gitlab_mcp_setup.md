# GitLab MCP Setup

Open Executive connects to GitLab.com's official MCP endpoint through a pinned
`mcp-remote` stdio bridge. The integration is read/write with two code-enforced
boundaries:

- `MCPGateway` permits only the exact reviewed live GitLab tool catalog.
- Writes must name a full project/group path or GitLab URL under a root listed
  in `GITLAB_WRITE_NAMESPACES`; numeric-only or out-of-namespace targets fail.
- Any unknown future GitLab tool fails closed until the code allowlist changes.
- OAuth state persists on the `/data` volume, outside the company document
  directory and outside model context.

The enabled write operations cover issues, epics and other work items, notes,
branches, commits, merge requests and reviews, pipelines, work-item links,
forks, and security scan profile attachment. The last operation accepts only
numeric targets upstream, so the namespace gate intentionally blocks it until
GitLab exposes a path that can be validated locally.

Configure one or more comma-separated write roots in the API environment. With
no value, reads remain available but every GitLab write fails closed:

```dotenv
GITLAB_HOST=gitlab.com
GITLAB_WRITE_NAMESPACES=your-group/your-subgroup,another-authorized-group
```

## 1. Enable GitLab MCP for the top-level group

As an Owner of the top-level GitLab group:

1. Set GitLab Duo availability to **Always on** or **On by default**.
2. Turn on experiment and beta GitLab Duo features.
3. Under **Settings → General → Permissions and group features → MCP client
   access**, select **Allow connection to GitLab**.

GitLab's endpoint is `https://gitlab.com/api/v4/mcp` and uses OAuth with the
`mcp` scope.

## 2. Build the API image

From the repository root:

```bash
docker compose --env-file .env -f docker/docker-compose.yml build api
```

The image contains Node 22 and the pinned `mcp-remote` bridge.

## 3. Bootstrap OAuth on the Docker host

GitLab does not advertise a device-authorization endpoint, so complete the
one-time browser flow on the host and then place the resulting OAuth cache on
the API volume:

```bash
export OE_GITLAB_AUTH_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/openexecutive/gitlab-mcp-auth"
mkdir -p "$OE_GITLAB_AUTH_DIR"
chmod 700 "$OE_GITLAB_AUTH_DIR"

MCP_REMOTE_CONFIG_DIR="$OE_GITLAB_AUTH_DIR" \
  npx -p mcp-remote@0.8.1 mcp-remote-client \
  https://gitlab.com/api/v4/mcp \
  --static-oauth-client-metadata '{"scope":"mcp"}' \
  --transport http-only
```

Review and approve the GitLab authorization page opened by the command. Never
copy token-file contents into chat, logs, or the repository.

## 4. Seed the API volume

With the API container running:

```bash
docker exec openexecutive-api-1 mkdir -p /data/mcp-auth/gitlab /data/company
docker cp "$OE_GITLAB_AUTH_DIR/." openexecutive-api-1:/data/mcp-auth/gitlab/
docker cp packages/core/mcp_servers.gitlab.json \
  openexecutive-api-1:/data/company/mcp_servers.json
docker exec openexecutive-api-1 chmod -R go-rwx /data/mcp-auth/gitlab
docker compose --env-file .env -f docker/docker-compose.yml \
  up -d --no-deps --force-recreate api
```

The settings layer automatically enables MCP when the configured file exists.

## 5. Verify

```bash
docker compose --env-file .env -f docker/docker-compose.yml logs api
```

Confirm the log contains `MCPGateway started`. Then ask Open Executive:

> List the open merge requests in acme/platform/org/branding and
> summarize their pipeline state.

For a write test, ask it to create an explicitly labelled test issue in
one of the configured namespaces, verify the issue, and then close it. Also ask
it to create an issue outside every configured namespace as a negative test.
The gateway must return a namespace error without calling GitLab.

## Rotation and recovery

`mcp-remote` refreshes the OAuth access token using the persisted refresh token.
If GitLab revokes the grant or refresh fails, repeat step 3 and copy the new
cache into the volume. Do not weaken the gateway catalog or namespace boundary
to work around an authentication error.
