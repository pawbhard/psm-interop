#!/usr/bin/env bash
# Copyright 2026 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Verifies that docker images in Artifact Registry for the last N version branches
# for gRPC languages (cpp, python, go, java) are tagged with the matching Git commit hash from GitHub.
#

set -euo pipefail

#######################################
# Check if required dependencies are installed.
# Returns:
#   0 if all dependencies are met, 1 otherwise.
#######################################
check_dependencies() {
  local missing=()
  local cmd
  for cmd in gh gcloud jq; do
    if ! command -v "${cmd}" &> /dev/null; then
      missing+=("${cmd}")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Error: Missing required dependencies: ${missing[*]}" >&2
    return 1
  fi
  return 0
}

#######################################
# Display usage information and exit.
# Outputs:
#   Writes usage instructions to stderr.
#######################################
display_usage() {
  cat <<EOF >/dev/stderr
Verifies that docker images in Artifact Registry for the last N version branches
for gRPC languages (cpp, python, go, java) are tagged with the matching Git commit hash from GitHub.

USAGE: $0 [options]

OPTIONS:
   -n, --num-branches NUM   Number of recent version branches to verify per language (default: 3)
   -r, --registry REGISTRY  Artifact Registry path (default: us-docker.pkg.dev/grpc-testing/psm-interop)
   -l, --languages LANGS    Comma-separated list of languages to verify (default: cpp,python,go,java)
   -m, --include-master     Include the master branch in verification
   -h, --help               Show this help message

EXAMPLES:
   $0
   $0 -n 3
   $0 --languages cpp,go
   $0 --include-master
EOF
  exit 1
}

#######################################
# Get the GitHub repository for a given language.
# Arguments:
#   Language string (e.g., cpp, go).
# Outputs:
#   Writes the repository name (e.g., grpc/grpc) to stdout.
#######################################
get_repo_for_lang() {
  local lang="$1"
  case "${lang}" in
    cpp|python) echo "grpc/grpc" ;;
    go) echo "grpc/grpc-go" ;;
    java) echo "grpc/grpc-java" ;;
    *) echo "" ;;
  esac
}

#######################################
# Fetch version branches and their SHAs from GitHub using GraphQL.
# Arguments:
#   Repository name (e.g., grpc/grpc).
#   Number of branches to fetch.
#   Include master flag ("true" or "false").
# Outputs:
#   Writes lines of "branch_name commit_sha" to stdout.
#######################################
fetch_version_branches() {
  local repo="$1"
  local count="$2"
  local include_master="$3"
  local owner repo_name raw_branches master_branch

  IFS='/' read -r owner repo_name <<< "${repo}"

  if [[ "${include_master}" == "true" ]]; then
    master_branch=$(gh api "repos/${repo}/branches/master" --jq '"master \(.commit.sha)"')
    if [[ -n "${master_branch}" ]]; then
      echo "${master_branch}"
    fi
  fi

  if [[ "${count}" -gt 0 ]]; then
    local gql_query
    read -r -d '' gql_query << 'QUERY' || true
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", query: "v1.", first: 100) {
      nodes {
        name
        target {
          ... on Commit {
            oid
          }
        }
      }
    }
  }
}
QUERY
    raw_branches=$(gh api graphql -F owner="${owner}" -F name="${repo_name}" -f query="${gql_query}" \
      --jq '.data.repository?.refs?.nodes[]? | select(.name | test("^v1\\.[0-9]+\\.[xX]$")) | "\(.name) \(.target.oid)"')

    if [[ -n "${raw_branches}" ]]; then
      printf '%s\n' "${raw_branches}" | sort -t. -k2,2nr | head -n "${count}"
    fi
  fi
}

#######################################
# Verify tag for a specific image against an expected branch and SHA.
# Arguments:
#   Registry path.
#   Language.
#   Image role (client/server).
#   Tags JSON string for this image.
#   Branch name.
#   Expected SHA.
# Returns:
#   0 if verification passed, 1 otherwise.
#######################################
verify_image_tag() {
  local registry="$1"
  local lang="$2"
  local role="$3"
  local tags_json="$4"
  local branch="$5"
  local sha="$6"
  local result has_sha actual_shas all_tags

  # Use jq to extract status, actual SHAs, and all tags in a single pipe-separated line.
  result=$(jq -r --arg branch "${branch}" --arg sha "${sha}" \
    '.[] | select((.tags // []) | index($branch) != null) | [
      ((.tags // []) | index($sha) != null),
      ([.tags[]? | select(test("^[0-9a-f]{40}$"))] | join(",")),
      ((.tags // []) | join(","))
    ] | join("|")' <<< "${tags_json}")

  if [[ -z "${result}" ]]; then
    echo "❌ [FAIL] https://${registry}/${lang}-${role}:${branch} -> Branch tag missing"
    return 1
  fi

  IFS='|' read -r has_sha actual_shas all_tags <<< "${result}"

  if [[ "${has_sha}" == "true" ]]; then
    echo "✅ [PASS] https://${registry}/${lang}-${role}:${branch} -> ${sha}"
    return 0
  else
    if [[ -n "${actual_shas}" ]]; then
      echo "❌ [FAIL] https://${registry}/${lang}-${role}:${branch} -> ${actual_shas} (Expected ${sha})"
    else
      echo "❌ [FAIL] https://${registry}/${lang}-${role}:${branch} -> No SHA (Expected ${sha}, tags: ${all_tags})"
    fi
    return 1
  fi
}

main() {
  local num_branches=3
  local registry="us-docker.pkg.dev/grpc-testing/psm-interop"
  local languages="cpp,python,go,java"
  local include_master=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--num-branches|-r|--registry|-l|--languages)
        [[ $# -ge 2 ]] || { echo "Error: Option $1 requires an argument." >&2; display_usage; }
        case "$1" in
          -n|--num-branches) num_branches="$2" ;;
          -r|--registry)     registry="$2" ;;
          -l|--languages)    languages="$2" ;;
        esac
        shift 2
        ;;
      -m|--include-master)
        include_master=true
        shift 1
        ;;
      -h|--help)
        display_usage
        ;;
      *)
        echo "Unknown argument: $1" >&2
        display_usage
        ;;
    esac
  done

  if [[ ! "${num_branches}" =~ ^[0-9]+$ ]]; then
    echo "Error: --num-branches must be a non-negative integer, got '${num_branches}'" >&2
    display_usage
  fi

  if ! check_dependencies; then
    return 1
  fi

  local SCRIPT_DIR XDS_K8S_DRIVER_DIR
  SCRIPT_DIR="$( cd -- "$(dirname "$0")" >/dev/null 2>&1 ; pwd -P )"
  XDS_K8S_DRIVER_DIR="${SCRIPT_DIR}/.."

  cd "${XDS_K8S_DRIVER_DIR}"

  local lang_array
  IFS=',' read -r -a lang_array <<< "${languages}"

  local overall_passed=true
  local lang repo branches role
  declare -A branch_cache

  echo "======================================================================"
  echo "Verifying Image Tags for Last ${num_branches} Version Branches"
  echo "Registry: ${registry}"
  echo "======================================================================"

  for lang in "${lang_array[@]}"; do
    lang=$(printf '%s\n' "${lang}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    repo=$(get_repo_for_lang "${lang}")

    if [[ -z "${repo}" ]]; then
      printf "\nSkipping unknown language: %s\n" "${lang}"
      continue
    fi

    printf "\n--- Language: %s (repo: %s) ---\n" "$(printf '%s\n' "${lang}" | tr '[:lower:]' '[:upper:]')" "${repo}"

    local cache_key="${repo}_${num_branches}_${include_master}"
    if [[ -z "${branch_cache[${cache_key}]:-}" ]]; then
      branch_cache[${cache_key}]=$(fetch_version_branches "${repo}" "${num_branches}" "${include_master}")
    fi
    branches="${branch_cache[${cache_key}]}"

    if [[ -z "${branches}" ]]; then
      echo "Error: No version branches found for ${repo}"
      overall_passed=false
      continue
    fi

    local client_tags_json server_tags_json
    if ! client_tags_json=$(gcloud container images list-tags "${registry}/${lang}-client" --format="json" 2>/dev/null) || \
       ! server_tags_json=$(gcloud container images list-tags "${registry}/${lang}-server" --format="json" 2>/dev/null); then
      echo "❌ [FAIL] Failed to list image tags for ${registry}/${lang}. Ensure images exist and permissions are granted." >&2
      overall_passed=false
      continue
    fi

    local first_branch=true
    while read -r branch sha; do
      [[ -z "${branch}" ]] && continue
      if ${first_branch}; then
        first_branch=false
      else
        echo ""
      fi
      echo "Branch ${branch} (Git SHA: ${sha})"

      if ! verify_image_tag "${registry}" "${lang}" "client" "${client_tags_json}" "${branch}" "${sha}"; then
        overall_passed=false
      fi
      if ! verify_image_tag "${registry}" "${lang}" "server" "${server_tags_json}" "${branch}" "${sha}"; then
        overall_passed=false
      fi
    done <<< "${branches}"
  done

  echo "======================================================================"
  if ${overall_passed}; then
    echo "🎉 RESULT: ALL IMAGE TAG VERIFICATIONS PASSED 🎉"
    return 0
  else
    echo "💥 RESULT: IMAGE TAG VERIFICATION FAILED 💥"
    return 1
  fi
}

main "$@"
