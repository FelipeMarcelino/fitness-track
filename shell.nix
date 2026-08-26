{
  pkgs,
  lib,
  stdenv,
  ...
}:
let
  pkgs-unfree = import pkgs.path {
    inherit (pkgs) system;
    config.allowUnfree = true;
  };
  pythonPackages = pkgs-unfree.python313Packages;
in
pkgs-unfree.mkShell {
  buildInputs = [
    pythonPackages.python
    pythonPackages.venvShellHook
    pkgs-unfree.autoPatchelfHook

    # Python dependency management (WS-1, decision D1).
    pkgs-unfree.uv

    # `make` drives fmt / lint / typecheck / test.
    pkgs-unfree.gnumake

    # No docker CLI here on purpose: on WSL the client comes from Docker
    # Desktop's integration (/usr/bin/docker) and a second client in the
    # devshell shadows it with one that does not know the Desktop context.
  ];
  venvDir = "./.venv";
  postVenvCreation = ''
    unset SOURCE_DATE_EPOCH
    autoPatchelf ./.venv
  '';
  postShellHook = ''
    unset SOURCE_DATE_EPOCH
    export LD_LIBRARY_PATH=${lib.makeLibraryPath [ stdenv.cc.cc ]}:$LD_LIBRARY_PATH

    # Point uv at the venv venvShellHook already created instead of letting it
    # build a second one, and forbid it from downloading a Python other than
    # the interpreter Nix pinned above.
    export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
    export UV_PYTHON_DOWNLOADS=never
    export UV_PYTHON="${pythonPackages.python}/bin/python3.13"
  '';
}
