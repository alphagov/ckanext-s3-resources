'''Adds paster command to migrate existing CKAN resources to S3'''
import datetime
import logging

import ckan.model as model
import ckan.lib.cli as cli
import ckan.plugins.toolkit as toolkit
import ckan.logic as logic
from pylons import config

import ckanext.datagovsg_s3_resources.upload as upload


class MigrateToS3(cli.CkanCommand):
    '''Migrate existing resources to S3

      Usage:
          migrate_s3 - uploads all resources that are currently not on S3
            to S3 and updates the URL on CKAN

          migrate_s3 force_s3 - uploads ALL resources to S3

    '''
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 1
    min_args = 0

    def command(self):
        '''Runs on the migrate_s3 command'''
        self._load_config()

        skip_existing_s3_upload = True

        if len(self.args) > 0:
            if self.args[0] == 'force_s3':
                skip_existing_s3_upload = False

        user = toolkit.get_action('get_site_user')({'model': model, 'ignore_auth': True}, {})
        context = {
            'ignore_auth': True
        }

        # dataset_names (list) - list of dataset names
        # key_errors (int) - count of key errors encountered during migration
        # validation_errors (int) - count of validation errors encountered during migration
        # other_errors_list (list) - list of (non-key and non-validation) errors encountered during migration
        # pkg_crashes (set) - set of package IDs of packages that encountered errors during migration
        # pkg_crashes_w_error (list) - list of dicts with two fields: 'pkg_name' and 'error'
        dataset_names = toolkit.get_action('package_list')(context, {})
        key_errors = 0
        validation_errors = 0
        other_errors_list = []
        pkg_crashes = set()
        pkg_crashes_w_error = []

        # blacklist (list) - list of filetypes that we want to avoid uploading
        # Obtain the space separated string from config, then split to obtain a list
        # and convert elements to lowercase
        blacklist = config.get('ckan.datagovsg_s3_resources.upload_filetype_blacklist', '').split()
        blacklist = [t.lower() for t in blacklist]

        # blacklisted (list) - Resources that have blacklisted filetypes. 
        #                      List of dicts with two fields: 'resource_id' and 'extension'
        # not_blacklist (int) - count of resources that have blacklisted filetypes
        # already_on_s3 (list) - List of resource IDs of resources that are already uploaded to S3
        # extensions_seen (set) - set of all filetypes that exist in our database
        blacklisted = []
        not_blacklisted = 0
        already_on_s3 = []
        extensions_seen = set()

        # Obtain logger
        logger = logging.getLogger(__name__)

        for dataset_name in dataset_names:
            logger.info("Starting package migration to S3 for package %s" % dataset_name)
            try:
                pkg = toolkit.get_action('package_show')(context, {'id': dataset_name})
                if pkg.get('num_resources') > 0:
                    for resource in pkg.get('resources'):
                        # If the resource is already uploaded to S3, don't reupload
                        if skip_existing_s3_upload and resource['url_type'] == 's3':
                            logger.info("Resource %s is already on S3, skipping to next resource." % resource.get('name', ''))
                            already_on_s3.append(resource['id'])
                            continue
                        # If filetype of resource is blacklisted, skip the upload to S3
                        extension = resource['format'].lower()
                        extensions_seen.add(extension)
                        if extension not in blacklist:
                            not_blacklisted += 1
                            try:
                                logger.info("Attempting to migrate resource %s to S3..." % resource.get('name', ''))
                                self.change_to_s3(context, resource)
                                logger.info("Successfully migrated resource %s to S3." % resource.get('name', ''))
                            except logic.ValidationError:
                                logger.error("Validation Error when migrating resource %s" % resource.get('name', ''))
                                validation_errors += 1
                                pkg_crashes.add(pkg['id'])
                            except KeyError:
                                logger.error("Key Error when migrating resource %s" % resource.get('name', ''))
                                key_errors += 1
                                pkg_crashes.add(pkg['id'])
                            except Exception as error:
                                logger.error("Error when migrating resource %s - %s" % (resource.get('name', ''), error))
                                other_errors_list.append({'id': pkg['id'], 'error': error})
                                pkg_crashes.add(pkg['id'])
                        else:
                            logger.info("Resource %s is blacklisted, skipping to next resource." % resource.get('name', ''))
                            blacklisted.append({'resource_id': resource['id'], 'id': extension})
                # Upload package zipfile to S3 after all the resources have been updated
                upload.upload_package_zipfile_to_s3(context, pkg)
            except Exception as error:
                logger.error("Error when migrating package %s with error %s" % (dataset_name, error))
                pkg_crashes_w_error.append({'pkg_name': dataset_name, 'error': error})


        logger.info("NUMBER OF KEY ERROR CRASHES = %d" %  key_errors)
        logger.info("NUMBER OF VALIDATION ERROR CRASHES = %d" % validation_errors)
        logger.info("NUMBER OF OTHER ERROR CRASHES = %d" % len(other_errors_list))
        logger.info("NUMBER OF PACKAGE CRASHES = %d" % len(pkg_crashes))
        logger.info("PACKAGE_IDs = %s" % pkg_crashes)
        logger.info("OTHER ERRORS = %s" % other_errors_list)
        logger.info("ALREADY ON S3 = %s" % already_on_s3)
        logger.info("NOT BLACKLISTED = %d" % not_blacklisted)
        logger.info("BLACKLISTED = %s" % blacklisted)
        logger.info("EXTENSIONS SEEN = %s" % extensions_seen)
        logger.info("PACKAGE CRASHES WITH ERROR MESSAGE = \n%s" % pkg_crashes_w_error)

    def change_to_s3(self, context, resource):
        '''
        1. Uploads resource to S3
        2. Peforms resource_update
        3. Uploads the updated resource zipfile to S3
        '''
        toolkit.get_action('resource_update')(context, resource)
