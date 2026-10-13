{
  description = "Agent Dispatch: opens an agent session in a Herdr tab for each Soma task marked dispatch Requested (always-on Mac, outbound long poll)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, uv2nix, pyproject-nix, pyproject-build-systems }:
    let
      inherit (nixpkgs) lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Load the uv workspace (pyproject.toml + uv.lock) and build an overlay of
      # all locked dependencies. Prefer prebuilt wheels (matters on darwin).
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      # Per-package build fixups go here if a dep doesn't build cleanly.
      pyprojectOverrides = _final: _prev: { };

      mkPythonSet = pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]
        );
    in
    {
      # The job as a self-contained venv in the store - the Mac runs THIS (via
      # the darwin module), never a repo checkout. herdr and quota-axi are NOT
      # bundled: they must be the user's own installs (the running Herdr server's
      # client, the agents' quota), found on the daemon's PATH.
      packages = forAllSystems (pkgs: rec {
        agent-dispatch = (mkPythonSet pkgs).mkVirtualEnv "agent-dispatch-env" workspace.deps.default;
        default = agent-dispatch;
      });

      darwinModules.default = import ./nix/darwin.nix self;

      checks = forAllSystems (pkgs: {
        cli = pkgs.runCommand "agent-dispatch-cli" { } ''
          ${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/agent-dispatch --help
          touch $out
        '';
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
