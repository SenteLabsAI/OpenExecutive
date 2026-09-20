package executive.tools

default allow := false

allow if {
  input.class == "read"
  input.mcp_enabled == true
  not write_name
}

write_name if {
  regex.match("(?i)(delete|drop|destroy|trash|send|mail|slack|write|update|create)", input.tool_name)
}
