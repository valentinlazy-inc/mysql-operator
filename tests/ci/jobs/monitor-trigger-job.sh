#!/bin/bash
# Copyright (c) 2022, Oracle and/or its affiliates.
#
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
#
# monitors development branches that are not watched at concourse
# then triggers a build in a proper pipeline depending on the branch name
# the git branches monitored at concourse are (in theory): dev, qa, itch, and trunk. In practice, it is trunk only,
# but it shall change in the future, hopefully
set -vx

source $WORKSPACE/tests/ci/jobs/auxiliary/set-env.sh || return

echo "GIT_COMMIT: ${GIT_COMMIT}"
echo "GIT_COMMITTER_NAME: ${GIT_COMMITTER_NAME}"
echo "GIT_COMMITTER_EMAIL: ${GIT_COMMITTER_EMAIL}"
echo "GIT_URL: ${GIT_URL}"
echo "GIT_URL_N: ${GIT_URL_N}"
echo "GIT_BRANCH: ${GIT_BRANCH}"
echo "GIT_LOCAL_BRANCH: ${GIT_LOCAL_BRANCH}"
echo "GIT_PREVIOUS_COMMIT: ${GIT_PREVIOUS_COMMIT}"
echo "GIT_PREVIOUS_SUCCESSFUL_COMMIT: ${GIT_PREVIOUS_SUCCESSFUL_COMMIT}"

# jenkins regex filter for dev branch: ":^(?!origin/trunk$|origin/itch$|origin/dev$|origin/qa$|origin/s?qa-.*$).+$"

BRANCH_SHORT_NAME=${GIT_BRANCH#"origin/"}

case "${BRANCH_SHORT_NAME}" in
dev|trunk)
	# skip, should be triggered by concourse
	;;
itch)
	PIPELINE_NAME="itch"
	;;
qa)
	PIPELINE_NAME="qa"
	;;
ci/experimental/*)
	# skip triggering build for an experimental branch related to changes in CI
	;;
qa*|sqa*|ci/*)
	PIPELINE_NAME="qa"
	;;
*)
	PIPELINE_NAME="dev"
	;;
esac

if [[ -z $PIPELINE_NAME ]]; then
	echo "skipped ${GIT_BRANCH} $GIT_COMMIT"
	exit 0
fi

OPERATOR_GIT_REVISION=$GIT_COMMIT
OPERATOR_DEV_IMAGE_TAG=$(git describe --tags)'-dev'
OPERATOR_IMAGE=$LOCAL_REGISTRY_ADDRESS/$LOCAL_REPOSITORY_NAME/$COMMUNITY_OPERATOR_IMAGE_NAME:$OPERATOR_DEV_IMAGE_TAG
OPERATOR_ENTERPRISE_IMAGE=$LOCAL_REGISTRY_ADDRESS/$LOCAL_REPOSITORY_NAME/$ENTERPRISE_OPERATOR_IMAGE_NAME:$OPERATOR_DEV_IMAGE_TAG

JOB_PARAMS="OPERATOR_GIT_REVISION=${OPERATOR_GIT_REVISION}&OPERATOR_IMAGE=${OPERATOR_IMAGE}"
JOB_PARAMS="${JOB_PARAMS}&OPERATOR_ENTERPRISE_IMAGE=${OPERATOR_ENTERPRISE_IMAGE}&OPERATOR_INTERNAL_BUILD=true"

JOB_LINK=${JOB_PREFIX}/${PIPELINE_NAME}
curl -X POST -u ${JENKINS_USER_CRED} ${JOB_LINK}/buildWithParameters?${JOB_PARAMS}
