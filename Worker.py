#!/usr/bin/python
import boto3
import logging
import time
from distutils.util import strtobool
from SSMDelegate import SSMDelegate
import botocore

__author__ = "Gary Silverman"

class Worker(object):
	SNS_SUBJECT_PREFIX_WARNING="Warning:"
	SNS_SUBJECT_PREFIX_INFORMATIONAL="Info:"

	def __init__(self, workloadRegion, snsNotConfigured, snsTopic, snsTopicSubject, instance, logger, dryRunFlag):

		self.workloadRegion=workloadRegion
		self.instance=instance
		self.dryRunFlag=dryRunFlag
		self.logger = logger

		self.snsTopic = snsTopic
		self.snsTopicSubject = snsTopicSubject
		self.snsNotConfigured = snsNotConfigured

		self.instanceStateMap = {
			"pending" : 0, 
			"running" : 16,
			"shutting-down" : 32,
			"terminated" : 48,
			"stopping" : 64,
			"stopped" : 80
		}

		try:
			self.ec2Resource = boto3.resource('ec2', region_name=self.workloadRegion)
		except Exception as e:
			msg = 'Worker::__init__() Exception obtaining botot3 ec2 resource in region %s -->' % workloadRegion
			self.logger.error(msg + str(e))

	def publishSNSTopicMessage(self, subjectPrefix, theMessage, instance):
		tagsMsg=''
		if( instance is not None ):
			for tag in instance.tags:
				tagsMsg = tagsMsg + '\nTag {0}=={1}'.format( str(tag['Key']), str(tag['Value']) )

		try:
			self.snsTopic.publish(
				Subject=self.snsTopicSubject + ':' + subjectPrefix,
				Message=theMessage + tagsMsg,
			)

		except Exception as e:
			self.logger.error('publishSNSTopicMessage() ' + str(e) )



class StartWorker(Worker):

	def __init__(self, ddbRegion, workloadRegion, snsNotConfigured, snsTopic, snsTopicSubject, instance, all_elbs, logger, dryRunFlag):
		super(StartWorker, self).__init__(workloadRegion, snsNotConfigured, snsTopic, snsTopicSubject, instance, all_elbs, logger, dryRunFlag)

		self.ddbRegion=ddbRegion

	def addressELBRegistration(self):

		try:
			for i in self.all_elbs['LoadBalancerDescriptions']:
				for j in i['Instances']:
					if j['InstanceId'] == self.instance.id:
						self.elb_name = i['LoadBalancerName']
						self.logger.info("Instance %s is attached to ELB %s" % self.instance.id, self.elb_name)
			try:
				self.elb.deregister_instances_from_load_balancer(LoadBalancerName=self.elb_name,Instances=[{'InstanceId': self.instance.id}])
				self.logger.info("Succesfully deregistered instance %s from load balancer %s" % self.instance.id, self.elb_name)
			except botocore.exceptions.ClientError as e:
				self.logger.info("Could not deregistered instance %s from load balancer %s" % self.instance.id,self.elb_name)
			try:
				self.elb.register_instances_with_load_balancer(LoadBalancerName=self.elb_name, Instances=[{'InstanceId': self.instance.id}])
				self.logger.info("Succesfully registered instance %s to load balancer %s" % self.instance.id, self.elb_name)
			except botocore.exceptions.ClientError as e:
				self.logger.info('Could not register instance [%s] to load balancer [%s] because of [%s]' % (
				self.instance.id, self.elb_name, e)
		except:
			self.logger.info('Instance [%s] does not have any Load balancer, will just start it]' % (self.instance.id))


	def startInstance(self):

		result='Instance not started'
		if( self.dryRunFlag ):
			self.logger.warning('DryRun Flag is set - instance will not be started')
		else:
			try:
				StartWorker.addressELBRegistration()
				result=self.instance.start()
				self.logger.info('startInstance() for ' + self.instance.id + ' result is %s' % result)
			except Exception as e:
				self.logger.warning('Worker::instance.start() encountered an exception of -->' + str(e))

	def scaleInstance(self, modifiedInstanceType):
		instanceState = self.instance.state

		if (instanceState['Name'] == 'stopped'):

			result='no result'
			if( self.dryRunFlag ):
				self.logger.warning('DryRun Flag is set - instance will not be scaled')
			else:
				try:
					self.logger.info('Instance [%s] will be scaled to Instance Type [%s]' % (self.instance.id , modifiedInstanceType) )
					# EC2.Instance.modify_attribute()
					result=self.instance.modify_attribute(
						InstanceType={
							'Value': modifiedInstanceType
						}
					)
					# It appears the start instance reads 'modify_attribute' changes as eventually consistent in AWS (assume DynamoDB),
					#    this can cause an issue on instance type change, whereby the LaunchPlan generates an exception.
					#    To mitigate against this, we will introduce a one second sleep delay after modifying an attribute
					time.sleep(1.0)
				except Exception as e:
					self.logger.warning('Worker::instance.modify_attribute() encountered an exception where requested instance type ['+ modifiedInstanceType +'] resulted in -->' + str(e))

				self.logger.info('scaleInstance() for ' + self.instance.id + ' result is %s' % result)

		else:
			logMsg = 'scaleInstance() requested to change instance type for non-stopped instance ' + self.instance.id + ' no action taken'
			self.logger.warning(logMsg)

	def start(self):
		self.startInstance()

class StopWorker(Worker):
	def __init__(self, ddbRegion, workloadRegion, snsNotConfigured, snsTopic, snsTopicSubject, instance, logger, dryRunFlag):
		super(StopWorker, self).__init__(workloadRegion, snsNotConfigured, snsTopic, snsTopicSubject, instance, logger, dryRunFlag)
		
		self.ddbRegion=ddbRegion

		# MUST convert string False to boolean False
		self.waitFlag=strtobool('False')
		self.overrideFlag=strtobool('False')
		
	def stopInstance(self):

		self.logger.debug('Worker::stopInstance() called')

		result='Instance not Stopped'
		
		if( self.dryRunFlag ):
			self.logger.warning('DryRun Flag is set - instance will not be stopped')
		else:
			try:
				# EC2.Instance.stop()
				result = self.instance.stop()
			except Exception as e:
				self.logger.warning('Worker:: instance.stop() encountered an exception of -->' + str(e))

			self.logger.info('stopInstance() for ' + self.instance.id + ' result is %s' % result)

		# If configured, wait for the stop to complete prior to returning
		self.logger.debug('The bool value of self.waitFlag %s, is %s' % (self.waitFlag, bool(self.waitFlag)))

		
		# self.waitFlag has been converted from str to boolean via set method
		if( self.waitFlag ):
			self.logger.info(self.instance.id + ' :Waiting for Stop to complete...')

			if( self.dryRunFlag ):			
				self.logger.warning('DryRun Flag is set - waiter() will not be employed')
			else:
				try:
					# Need the Client to get the Waiter
					ec2Client=self.ec2Resource.meta.client
					waiter=ec2Client.get_waiter('instance_stopped')

					# Waits for 40 15 second increments (e.g. up to 10 minutes)
					waiter.wait( )

				except Exception as e:
					self.logger.warning('Worker:: waiter block encountered an exception of -->' + str(e))

		else:
			self.logger.info(self.instance.id + ' Wait for Stop to complete was not requested')
		
	def setWaitFlag(self, flag):

		# MUST convert string False to boolean False
		self.waitFlag = strtobool(flag)

	def getWaitFlag(self):
		return( self.waitFlag )
	
	def isOverrideFlagSet(self, S3BucketName, S3KeyPrefixName, overrideFileName, osType):
		''' Use SSM to check for existence of the override file in the guest OS.  If exists, don't Stop instance but log.
		Returning 'True' means the instance will not be stopped.  
			Either because the override file exists, or the instance couldn't be reached
		Returning 'False' means the instance will be stopped (e.g. not overridden)
		'''


		# If there is no overrideFilename specified, we need to return False.  This is required because the
		# remote evaluation script may evaluate to "Bypass" with a null string for the override file.  Keep in 
		# mind, DynamodDB isn't going to enforce an override filename be set in the directive.
		if not overrideFileName:
			self.logger.info(self.instance.id + ' Override Flag not set in specification.  Therefore this instance will be actioned. ')
			return False

		if not osType:
			self.logger.info(self.instance.id + ' Override Flag set BUT no Operating System attribute in specification. Therefore this instance will be actioned.')
			return False


		# Create the delegate
		ssmDelegate = SSMDelegate(self.instance.id, S3BucketName, S3KeyPrefixName, overrideFileName, osType, self.ddbRegion, self.logger, self.workloadRegion)


		# Very first thing to check is whether SSM is going to write the output to the S3 bucket.  
		#   If the bucket is not in the same region as where the instances are running, then SSM doesn't write
		#   the result to the bucket and thus the rest of this method is completely meaningless to run.

		if( ssmDelegate.isS3BucketInWorkloadRegion() == SSMDelegate.S3_BUCKET_IN_CORRECT_REGION ):

			warningMsg=''
			msg=''
			# Send request via SSM, and check if send was successful
			ssmSendResult=ssmDelegate.sendSSMCommand()
			if( ssmSendResult ):
				# Have delegate advise if override file was set on instance.  If so, the instance is not to be stopped.
				overrideRes=ssmDelegate.retrieveSSMResults(ssmSendResult)
				self.logger.debug('SSMDelegate retrieveSSMResults() results :' + overrideRes)

				if( overrideRes == SSMDelegate.S3_BUCKET_IN_WRONG_REGION ):
					# Per SSM, the bucket must be in the same region as the target instance, otherwise the results will not be writte to S3 and cannot be obtained.
					self.overrideFlag=True
					warningMsg= Worker.SNS_SUBJECT_PREFIX_WARNING + ' ' + self.instance.id + ' Instance will be not be stopped because the S3 bucket is not in the same region as the workload'
					self.logger.warning(warningMsg)

				elif( overrideRes == SSMDelegate.DECISION_STOP_INSTANCE ):
					# There is a result and it specifies it is ok to Stop
					self.overrideFlag=False
					self.logger.info(self.instance.id + ' Instance will be stopped')

				elif( overrideRes == SSMDelegate.DECISION_NO_ACTION_UNEXPECTED_RESULT ):
					# Unexpected SSM Result, see log file.  Will default to overrideFlag==true out of abundance for caution
					self.overrideFlag=True
					warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING +  ' ' + self.instance.id + ' Instance will be not be stopped as there was an unexpected SSM result.'
					self.logger.warning(warningMsg)

				elif( overrideRes == SSMDelegate.DECISION_RETRIES_EXCEEDED ):
					self.overrideFlag=True
					warningMsg = Worker.SNS_SUBJECT_PREFIX_WARNING +  ' ' + self.instance.id + ' Instance will be not be stopped # retries to collect SSM result from S3 was exceeded'
					self.logger.warning(warningMsg)

				else:
					# Every other result means the instance will be bypassed (e.g. not stopped)
					self.overrideFlag=True
					msg=Worker.SNS_SUBJECT_PREFIX_INFORMATIONAL +  ' ' + self.instance.id + ' Instance will be not be stopped because override file was set'
					self.logger.info(msg)

			else:
				self.overrideFlag=True
				warningMsg=Worker.SNS_SUBJECT_PREFIX_WARNING +  ' ' + self.instance.id + ' Instance will be not be stopped because SSM could not query it'
				self.logger.warning(warningMsg)

		else:
			self.overrideFlag=True
			warningMsg=Worker.SNS_SUBJECT_PREFIX_WARNING + ' SSM will not be executed as S3 bucket is not in the same region as the workload. [' + self.instance.id + '] Instance will be not be stopped'
			self.logger.warning(warningMsg)

		if( self.snsNotConfigured == False ):
			if( self.overrideFlag == True ):
				if( warningMsg ):
					self.publishSNSTopicMessage(Worker.SNS_SUBJECT_PREFIX_WARNING, warningMsg, self.instance)
				else:
					self.publishSNSTopicMessage(Worker.SNS_SUBJECT_PREFIX_INFORMATIONAL, msg, self.instance)
		
		return( self.overrideFlag )

	def setOverrideFlagSet(self, overrideFlag):
		self.overrideFlag=strtobool(overrideFlag)


	def execute(self, S3BucketName, S3KeyPrefixName, overrideFileName, osType):
		if( self.isOverrideFlagSet(S3BucketName, S3KeyPrefixName, overrideFileName, osType) == False ):
			self.stopInstance()
