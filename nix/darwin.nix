# nix-darwin module: Agent Dispatch as a kept-alive launchd USER agent
# (`agent-dispatch watch`) holding an outbound long poll on a Soma hub
# subscription over tasks.dispatch. Each Requested task becomes an agent session
# in a new tab of the user's Herdr server. Nothing reaches into this machine.
#
# Options are generic; a consumer's config supplies the hub URL, its
# subscription and how to read the job's own credential.
#
# Runs in the login session (gui/<uid>) so credentialCommand can read the login
# Keychain (ssh-descended shells cannot) and herdr reaches the user's server.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.agent-dispatch;
  exe = lib.getExe' cfg.package "agent-dispatch";
  optional = name: value: lib.optionalAttrs (value != null) { ${name} = value; };

  environment = {
    HOME = "/Users/${cfg.user}";
    PATH = cfg.path;
    AGENT_DISPATCH_HUB_URL = cfg.hubUrl;
    AGENT_DISPATCH_SUBSCRIPTION_ID = cfg.subscriptionId;
    AGENT_DISPATCH_STATE_DIR = cfg.stateDir;
    AGENT_DISPATCH_HERDR_SESSION = cfg.herdrSession;
    AGENT_DISPATCH_CWD = cfg.cwd;
    AGENT_DISPATCH_PROMPT_FILE = cfg.promptFile;
  }
  // lib.optionalAttrs (cfg.credentialCommand != [ ]) {
    AGENT_DISPATCH_TOKEN_COMMAND = builtins.toJSON cfg.credentialCommand;
  }
  // optional "AGENT_DISPATCH_HERDR_WORKSPACE" cfg.herdrWorkspace
  // optional "AGENT_DISPATCH_DEFAULT_AGENT" cfg.defaultAgent
  // optional "AGENT_DISPATCH_FALLBACK_AGENT" cfg.fallbackAgent
  // lib.optionalAttrs (cfg.agents != null) {
    AGENT_DISPATCH_AGENTS = builtins.toJSON (
      lib.mapAttrs (_: a: lib.filterAttrs (_: v: v != null) a) cfg.agents
    );
  }
  // lib.optionalAttrs (cfg.minQuotaPercent != null) {
    AGENT_DISPATCH_MIN_QUOTA_PERCENT = toString cfg.minQuotaPercent;
  };

  # The CLI with this machine's settings, so `agent-dispatch prompt` and `once`
  # in a shell see the same configuration as the daemon.
  wrapper = pkgs.writeShellScriptBin "agent-dispatch" ''
    ${lib.concatStrings (lib.mapAttrsToList (k: v: "export ${k}=${lib.escapeShellArg v}\n") (removeAttrs environment [ "HOME" "PATH" ]))}
    exec ${exe} "$@"
  '';

  agentType = lib.types.submodule {
    options = {
      kind = lib.mkOption {
        type = lib.types.enum [ "claude" "codex" ];
        description = "Herdr agent kind.";
      };
      model = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Model id passed as --model (null: the agent's own default).";
      };
      effort = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Reasoning effort (claude --effort, codex model_reasoning_effort).";
      };
    };
  };
in
{
  options.services.agent-dispatch = {
    enable = lib.mkEnableOption "opening an agent session in Herdr for each Soma task marked dispatch Requested";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The installed job package.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      description = "Login user whose session runs the agent, its Herdr server and the agents.";
      example = "local-user";
    };

    hubUrl = lib.mkOption {
      type = lib.types.str;
      description = "HTTPS origin of the Soma hub.";
      example = "https://soma.example.workers.dev";
    };

    subscriptionId = lib.mkOption {
      type = lib.types.str;
      description = "Hub subscription on tasks.dispatch that this job consumes.";
    };

    credentialCommand = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        argv printing this job's own hub token, run in the agent's login session
        (e.g. a login-Keychain lookup). Never put the token itself in Nix.
      '';
      example = [ "/usr/bin/security" "find-generic-password" "-s" "agent-dispatch" "-a" "hub" "-w" ];
    };

    herdrSession = lib.mkOption {
      type = lib.types.str;
      default = "default";
      description = "Herdr server session that receives the agent tabs.";
    };

    herdrWorkspace = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Herdr workspace id for new tabs (null: Herdr's choice).";
      example = "w1";
    };

    cwd = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}";
      defaultText = lib.literalExpression ''"/Users/''${user}"'';
      description = "Directory each agent session starts in.";
    };

    promptFile = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}/.config/agent-dispatch/prompt.md";
      defaultText = lib.literalExpression ''"/Users/''${user}/.config/agent-dispatch/prompt.md"'';
      description = ''
        The prompt template ({task_id} and {title} are filled in). User state:
        absent means the built-in default; `agent-dispatch prompt > <file>` starts
        a copy to edit.
      '';
    };

    agents = lib.mkOption {
      type = lib.types.nullOr (lib.types.attrsOf agentType);
      default = null;
      description = ''
        Alias table: an alias named in a task title (whole words, longest first)
        picks that agent. Replaces the built-in table (opus, sonnet, fable 5.1,
        codex); null keeps it.
      '';
      example = lib.literalExpression ''{ opus = { kind = "claude"; model = "claude-opus-5-5"; }; codex = { kind = "codex"; effort = "medium"; }; }'';
    };

    defaultAgent = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Alias used while its provider has quota left (null: opus).";
    };

    fallbackAgent = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Alias used when the default's provider has none (null: codex).";
    };

    minQuotaPercent = lib.mkOption {
      type = lib.types.nullOr lib.types.number;
      default = null;
      description = "Least effective quota percent (quota-axi) that counts as available (null: 1).";
    };

    path = lib.mkOption {
      type = lib.types.str;
      default = "/etc/profiles/per-user/${cfg.user}/bin:/run/current-system/sw/bin:/usr/bin:/bin";
      defaultText = lib.literalExpression ''"/etc/profiles/per-user/''${user}/bin:/run/current-system/sw/bin:/usr/bin:/bin"'';
      description = "PATH for the daemon; must contain herdr and quota-axi (and the agents quota-axi probes).";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}/.local/state/agent-dispatch";
      defaultText = lib.literalExpression ''"/Users/''${user}/.local/state/agent-dispatch"'';
      description = "State and the log (XDG state dir).";
    };

    label = lib.mkOption {
      type = lib.types.str;
      default = "com.alexmiller.agent-dispatch";
      description = "launchd Label (immutable once shipped).";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ wrapper ];

    system.activationScripts.postActivation.text = lib.mkAfter ''
      # launchd opens StandardOutPath before the job runs; create it as the
      # user so no parent (~/.local/state) ends up owned by root.
      /usr/bin/sudo -u ${lib.escapeShellArg cfg.user} /bin/mkdir -p ${lib.escapeShellArg cfg.stateDir}
    '';

    launchd.user.agents.agent-dispatch.serviceConfig = {
      Label = cfg.label;
      ProgramArguments = [ exe "watch" ];
      EnvironmentVariables = environment;
      RunAtLoad = true;
      KeepAlive = true;
      # A crashing job restarts at most every 30 s; the job itself never spins.
      ThrottleInterval = 30;
      ProcessType = "Background";
      WorkingDirectory = cfg.stateDir;
      StandardOutPath = "${cfg.stateDir}/agent-dispatch.log";
      StandardErrorPath = "${cfg.stateDir}/agent-dispatch.log";
    };
  };
}
