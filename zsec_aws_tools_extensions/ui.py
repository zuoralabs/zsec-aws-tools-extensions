import argparse
from types import MappingProxyType
from typing import Dict, Optional, Iterable, Mapping
from toolz import assoc, merge
import uuid

from zsec_aws_tools.basic import AWSResource, get_account_id
from zsec_aws_tools.aws_lambda import FunctionResource
import zsec_aws_tools.iam as zaws_iam

import logging

from .deployment import collect_garbage

logger = logging.getLogger(__name__)


def get_resource_meta_description(res) -> Dict[str, str]:
    if isinstance(res, AWSResource):
        account_number = get_account_id(res.session)
        zrn = f'zrn:aws:{account_number}:{res.region_name}:{str(res.ztid).lower()}'
        return dict(
            zrn=zrn,
            account_number=account_number,
            region_name=res.region_name,
            ztid=str(res.ztid),
            name=res.name,
            index_id=res.index_id,
            type='{}.{}'.format(type(res).__module__, type(res).__name__),
        )
    else:
        raise NotImplementedError


def put_resource_nice(
        manager,
        resource: AWSResource,
        dependency_order: int,
        force: bool,
        put_resource_record: Optional[FunctionResource],
        deployment_id: uuid.UUID,
):
    """

    :param manager:
    :param resource:
    :param dependency_order: within a memory management scope, resources of higher dependency_order can only depend on
        resources of lower dependency_order.
    :param force:
    :param put_resource_record:
    :param deployment_id:
    :return:
    """
    if resource.config:
        print(f'applying: {resource.name}(ztid={resource.ztid}) : {type(resource).__name__}')
        resource.put(force=force)
        if put_resource_record and put_resource_record.exists and resource.exists:
            payload = merge(
                get_resource_meta_description(resource),
                dict(deployment_id=str(deployment_id).lower(),
                     manager=manager,
                     dependency_order=dependency_order))
            resp = put_resource_record.invoke(json_codec=True, Payload=payload)

            if resp:
                print(resp)


def delete_resource_nice(
        manager,
        resource: AWSResource,
        force: bool,
        delete_resource_record: Optional[FunctionResource]
):
    if force:
        raise NotImplementedError('Need to implement manager check for delete.')

    if resource.exists:
        if isinstance(resource, zaws_iam.Role):
            print('detaching policies')
            resource.detach_all_policies()
        print('deleting: ', resource)
        resource.delete()

        if delete_resource_record and delete_resource_record.exists and not resource.exists:
            resp = delete_resource_record.invoke(json_codec=True,
                                                 Payload=assoc(get_resource_meta_description(resource),
                                                               'manager', manager))
            if resp:
                print(resp)
    else:
        print('does not exist: ', resource)


def handle_cli_command(
        manager: str,
        resources: Iterable[AWSResource],
        support_gc: bool = False,
        gc_scope: Mapping[str, str] = None,
        put_resource_record: Optional[FunctionResource] = None,
        delete_resource_record: Optional[FunctionResource] = None,
        resources_by_zrn_table=None,
):
    """

    :param manager: Used for "memory management" for resources.
    :param resources: Resources to put.
    :param support_gc: Whether to support garbage collection.
    :param gc_scope: defines a filter on attributes of resources in order to be considered in-scope for this deployment.
        This limits the garbage collection scope.
        E.g. `{'manager': manager, 'account_number': '123456789000'}`
        limit the GC scope to only resources with the specified manager and in the specified account.
        Default is `None`. If scope is `None`, this function behaves as if scope were set to `{'manager': manager}`.
    :param put_resource_record:
    :param delete_resource_record:
    :param resources_by_zrn_table:
    :return:
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='subparser_name')
    apply_parser = subparsers.add_parser('apply')
    apply_parser.add_argument('--force', '-f', action='store_true',
                              help='take ownership and apply resource configs even if not initially owned')
    destroy_parser = subparsers.add_parser('destroy')
    destroy_parser.add_argument('--force', '-f', action='store_true',
                                help='destroy resources even if not owned')

    for subparser in (apply_parser, destroy_parser):
        subparser.add_argument('--only-ztids', nargs='+', action='extend', type=uuid.UUID,
                               help='Only apply/destroy resources with particular ztids. May affect depedencies and'
                                    'dependents. If specified, there will be no garbage collection.')

        subparser.add_argument('--deployment-id', nargs=1, action='extend', type=uuid.UUID,
                               help='deployment id for mark and sweep garbage collection')

        subparser.add_argument('--dry-gc', action='store_true',
                               help='do not garbage collect, only report. If --only-ztids` is specified, this flag '
                                    'is redundant because GC will be skipped.')

    args = parser.parse_args()
    force = args.subparser_name in ('apply', 'destroy') and args.force

    want_gc = support_gc and not args.only_ztids

    resource: AWSResource
    deployment_id = args.deployment_id or uuid.uuid4()
    nn = 0
    if args.subparser_name == 'apply' or (args.subparser_name is None):
        for nn, resource in enumerate(resources):
            if not args.only_ztids or resource.ztid in args.only_ztids:
                put_resource_nice(
                    manager, resource,
                    dependency_order=nn,
                    force=force,
                    put_resource_record=put_resource_record,
                    deployment_id=deployment_id,
                )

    elif args.subparser_name == 'destroy':
        for resource in resources:
            if not args.only_ztids or resource.ztid in args.only_ztids:
                delete_resource_nice(manager, resource, force=force, delete_resource_record=delete_resource_record)

    max_marked_dependency_order = nn

    if support_gc:
        assert manager and resources_by_zrn_table

        if want_gc:
            if gc_scope is None:
                gc_scope = {'manager': manager}
            collect_garbage(resources_by_zrn_table, gc_scope, deployment_id,
                            max_marked_dependency_order, args.dry_gc)
        else:
            print('no gc')
    else:
        print('gc not supported, skipping')
