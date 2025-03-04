// Copyright (c) 2022,2023, Oracle and/or its affiliates.
//
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/
//

def prepareCommunityImage(String operatorImage) {
	return operatorImage.replace("mysql-operator", "community-operator")
}

def getImageInfo(String operatorImage) {
	if (operatorImage) {
		return operatorImage
	}
	return "not specified, it will be built locally"
}

def isCIExperimentalBuild() {
	final CI_EXPERIMENTAL_BRANCH_PREFIX = 'ci/experimental/'
	return params.OPERATOR_GIT_REVISION.contains(CI_EXPERIMENTAL_BRANCH_PREFIX)
}

def initEnv() {
	env.WORKERS_FOLDER = 'Shell/KubernetesOperator/' + "${isCIExperimentalBuild() ? 'sandbox' : 'workers'}"
	env.BUILD_TRIGGERED_BY = "${params.OPERATOR_INTERNAL_BUILD ? 'internal' : 'concourse'}"
	env.LOG_SUBDIR = "build-${BUILD_NUMBER}"
	env.LOG_DIR = "${WORKSPACE}/${LOG_SUBDIR}"
	env.ARTIFACT_FILENAME = "${JOB_BASE_NAME}-${BUILD_NUMBER}-result.tar.bz2"
	env.ARTIFACT_PATH = "${WORKSPACE}/${ARTIFACT_FILENAME}"

	env.SLACK_CHANNEL = "${isCIExperimentalBuild() ? '#mysql-operator-ci' : '#mysql-operator-dev'}"
	env.BUILD_NOTIFICATION_HEADER = "${currentBuild.fullDisplayName} (<${env.BUILD_URL}|Open>)"
	env.COLOR_INFO = '#808080'

	env.GIT_AUTHOR_DATE = sh (script: "git log -1 --pretty='%an <%ae>, %ad' ${GIT_COMMIT}", returnStdout: true).trim()
	env.GIT_BRANCH_NAME = sh (script: "git name-rev --name-only ${GIT_COMMIT}", returnStdout: true).trim()
	env.GIT_COMMIT_SUBJECT = sh (script: "git log -1 --pretty=%s ${GIT_COMMIT}", returnStdout: true).trim()
	env.GIT_COMMIT_SHORT = sh (script: "git rev-parse --short HEAD", returnStdout: true).trim()

	env.OPERATOR_COMMUNITY_IMAGE = prepareCommunityImage("${params.OPERATOR_IMAGE}")

	env.BASE_IMAGE_INFO = getImageInfo("${params.OPERATOR_IMAGE}")
	env.COMMUNITY_IMAGE_INFO = getImageInfo("${env.OPERATOR_COMMUNITY_IMAGE}")
	env.ENTERPRISE_IMAGE_INFO = getImageInfo("${params.OPERATOR_ENTERPRISE_IMAGE}")

	env.TEST_RESULTS_UNAVAILABLE = 'UNAVAILABLE'
	env.TEST_RESULTS_SOME_AVAILABLE = 'SOME_AVAILABLE'
	env.TEST_RESULTS_ALL_AVAILABLE = 'ALL_AVAILABLE'
}

def getInitMessage() {
	return """${env.BUILD_NOTIFICATION_HEADER}
${currentBuild.getBuildCauses().shortDescription} (${env.BUILD_TRIGGERED_BY})
Branch/Revision: ${env.GIT_BRANCH_NAME} ${params.OPERATOR_GIT_REVISION}
Base Image: ${env.BASE_IMAGE_INFO}
Community Image: ${env.COMMUNITY_IMAGE_INFO}
Enterprise Image: ${env.ENTERPRISE_IMAGE_INFO}
The latest commit:
${env.GIT_AUTHOR_DATE}
${env.GIT_COMMIT} [hash: ${env.GIT_COMMIT_SHORT}]
${env.GIT_COMMIT_SUBJECT}"""
}

def addTestResults(String k8s_env, int expectedResultsCount) {
	sh "ls ${env.LOG_DIR}"
	def testResultsPattern = "$k8s_env-*-result.tar.bz2"
	def testResults = findFiles glob: "**/${env.LOG_SUBDIR}/$testResultsPattern"
	if (testResults.length == 0) {
		return env.TEST_RESULTS_UNAVAILABLE
	}

	def resultPattern = "${env.LOG_DIR}/$testResultsPattern"
	sh "cat $resultPattern | tar jxvf - -i -C ${env.LOG_DIR} && rm $resultPattern"

	// uncomment during Jenkins refactorings when some jobs are intentionally skipped
	// sh "touch ${LOG_DIR}/xml/*.xml"

	def summary = junit allowEmptyResults: true, testResults: "${env.LOG_SUBDIR}/xml/*$k8s_env-*.xml"
	echo "${summary.totalCount} tests, ${summary.passCount} passed, ${summary.failCount} failed, ${summary.skipCount} skipped"

	if (summary.totalCount > 0) {
		if (testResults.length == expectedResultsCount) {
			return env.TEST_RESULTS_ALL_AVAILABLE
		}
		return env.TEST_RESULTS_SOME_AVAILABLE
	}
	return env.TEST_RESULTS_UNAVAILABLE
}

def getMergedReports(String reportPattern) {
	def reports = findFiles glob: "**/${env.LOG_SUBDIR}/$reportPattern"
	if (reports.length == 0) {
		return ""
	}

	reportSummary = sh (script: "cat ${env.LOG_DIR}/$reportPattern | sort", returnStdout: true)
	echo reportSummary
	return reportSummary
}

def getMergedStatsReports() {
	statsPattern = "*-build-*-stats.log"
	return getMergedReports(statsPattern)
}

def getTestSuiteReport() {
	sh "ls ${env.LOG_DIR}"
	def testSuiteReportPattern = 'test_suite_report_*.tar.bz2'
	def testSuiteReportFiles = findFiles glob: "**/${env.LOG_SUBDIR}/$testSuiteReportPattern"
	if (testSuiteReportFiles.length == 0) {
		return ""
	}

	def reportPattern = "${env.LOG_DIR}/$testSuiteReportPattern"
	sh "cat $reportPattern | tar jxvf - -i -C ${env.LOG_DIR} && rm $reportPattern"

	def reportPath = "${env.LOG_DIR}/test_suite_report.txt"
	def reportExists = fileExists reportPath
	if (!reportExists) {
		return ""
	}

	testSuiteReport = "Test suite:\n"

	def briefReportPath = "${env.LOG_DIR}/test_suite_brief_report.txt"
	def ReportedErrorsMaxCount = 30
	sh "cat $reportPath | sed -ne '1,$ReportedErrorsMaxCount p' -e '${ReportedErrorsMaxCount+1} iand more...' > $briefReportPath"
	testSuiteReport += readFile(file: briefReportPath)

	echo testSuiteReport
	return testSuiteReport
}

def anyResultsAvailable() {
	return env.MINIKUBE_RESULT_STATUS == env.TEST_RESULTS_SOME_AVAILABLE ||
		env.MINIKUBE_RESULT_STATUS == env.TEST_RESULTS_ALL_AVAILABLE ||
		env.K3D_RESULT_STATUS == env.TEST_RESULTS_SOME_AVAILABLE ||
		env.K3D_RESULT_STATUS == env.TEST_RESULTS_ALL_AVAILABLE
}

def getMergedIssuesReports() {
	statsPattern = "*-build-*-issues.log"
	return getMergedReports(statsPattern)
}

def getTestsSuiteIssuesByEnv(String k8s_env, String result) {
	switch(result) {
		case env.TEST_RESULTS_ALL_AVAILABLE:
			return ""
		case env.TEST_RESULTS_SOME_AVAILABLE:
			return "Some test results for ${k8s_env} unavailable!\n"
		default:
			return "No test results for ${k8s_env}!\n"
	}
}

def getTestsSuiteIssues() {
	sh 'ls -lRF ${LOG_DIR}'
	def testSuiteIssues = ""
	if (anyResultsAvailable()) {
		testSuiteIssues += getTestsSuiteIssuesByEnv("minikube", env.MINIKUBE_RESULT_STATUS)
		testSuiteIssues += getTestsSuiteIssuesByEnv("k3d", env.K3D_RESULT_STATUS)
	} else {
		testSuiteIssues = "No test results available!\n"
	}

	testSuiteIssues += getMergedIssuesReports()

	if (testSuiteIssues) {
		testSuiteIssues = "Issues:\n$testSuiteIssues"
	}

	return testSuiteIssues
}

def getBuildDuration() {
	return "${currentBuild.durationString.minus(' and counting')}"
}

def getChangeLog() {
	def changeSets = currentBuild.changeSets
	def changeSetsCount = changeSets.size()
	if (!changeSetsCount) {
		return "No changes\n"
	}

	def changesLog = "Changes:\n"

	def allChangesCount = 0
	for (int i = 0; i < changeSetsCount; ++i) {
		allChangesCount += changeSets[i].items.length
	}

	def listOfChanges = []

	def ChangesMaxCount = 10
	def firstChangeIndex = 0
	if (allChangesCount > ChangesMaxCount) {
		listOfChanges.add("[...previous...]")
		firstChangeIndex = allChangesCount - ChangesMaxCount
	}

	def changeIndex = 0
	for (int i = 0; i < changeSetsCount; ++i) {
		def entries = changeSets[i].items
		for (int j = 0; j < entries.length; ++j) {
			if (changeIndex >= firstChangeIndex) {
				def entry = entries[j]
				listOfChanges.add("${entry.msg} [${entry.author}, ${new Date(entry.timestamp)}]")
			}
			++changeIndex
		}
	}

	changesLog += listOfChanges.reverse().join("\n")
	return changesLog
}

@NonCPS
def modifyBuildStatus(String status) {
	if (!env.BUILD_STATUS) {
		env.BUILD_STATUS = status
	} else {
		env.BUILD_STATUS += ", " + status
	}
}

def getTestResults() {
	if (!env.INIT_STAGE_SUCCEEDED) {
		return "Init (local registry) stage failed!"
	}

	if (params.OPERATOR_INTERNAL_BUILD && !env.BUILD_STAGE_SUCCEEDED) {
		return "Build dev-images stage failed!"
	}

	def testsSuiteIssues = "${env.TESTS_SUITE_ISSUES ? env.TESTS_SUITE_ISSUES + '\n' : ''}"

	if (env.TEST_SUITE_REPORT) {
		return testsSuiteIssues + env.TEST_SUITE_REPORT
	}

	return testsSuiteIssues + "Test stage failed!"
}

def getBuildResultColor() {
	buildStatus = env.BUILD_STATUS
	if (buildStatus.contains("failure") || buildStatus.contains("aborted")) {
		return "danger"
	}

	if (buildStatus.contains("unstable") || buildStatus.contains("regression") || buildStatus.contains("unsuccessful")) {
		return "warning"
	}

	if (buildStatus.contains("fixed") || buildStatus.contains("success")) {
		return "good"
	}

	// theoretically, that point of code shouldn't be reached, but just in case return 'good' by default
	return "good"
}

def getBuildSummary() {
	return """${env.BUILD_NOTIFICATION_HEADER}
Status: ${env.BUILD_STATUS}
Duration: ${env.BUILD_DURATION}"""
}

def getBuildStats() {
	def stats = getMergedStatsReports()
	if (!stats) {
		stats = "Not found!"
	}
	return "Stats:\n${stats}"
}

def getBuildAttachments() {
	def summaryColor = getBuildResultColor()
	def attachments = [
		[
			text: getBuildSummary(),
			color: summaryColor
		],
		[
			text: "${env.CHANGE_LOG}",
			color: summaryColor
		],
		[
			text: getTestResults(),
			color: summaryColor
		],
		[
			text: getBuildStats(),
			color: summaryColor
		]
	]
	return attachments
}

return this
