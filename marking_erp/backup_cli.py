import argparse
import json
import os
from pathlib import Path
from .backup import BackupError,create_backup,verify_backup,restore_backup
from .config import Settings
from .database import database_engine


def main():
    parser=argparse.ArgumentParser(description='Consistent ERP backup and isolated restore; never overwrites an existing database')
    actions=parser.add_subparsers(dest='action',required=True)
    create=actions.add_parser('create');create.add_argument('--output',type=Path,required=True)
    verify=actions.add_parser('verify');verify.add_argument('--bundle',type=Path,required=True)
    restore=actions.add_parser('restore');restore.add_argument('--bundle',type=Path,required=True);restore.add_argument('--new-database',required=True);restore.add_argument('--files-path',type=Path,required=True)
    args=parser.parse_args();engine=None
    try:
        if args.action=='verify':
            manifest=verify_backup(args.bundle);result={'valid':True,'schema_revision':manifest['schema_revision'],'file_count':len(manifest['files'])}
        elif args.action=='restore':
            admin=os.environ.get('ERP_RESTORE_ADMIN_URL')
            if not admin:raise BackupError('Set ERP_RESTORE_ADMIN_URL privately, using a maintenance database and CREATE DATABASE privileges')
            # Recovery needs no working source DSN or source server.
            settings=Settings(database_url=admin)
            result=restore_backup(settings,args.bundle,admin_url=admin,new_database=args.new_database,files_path=args.files_path)
        else:
            settings=Settings();engine=database_engine(settings);result=create_backup(settings,engine,args.output)
        print(json.dumps(result,ensure_ascii=False))
    except BackupError as error:parser.exit(1,str(error)+'\n')
    finally:
        if engine:engine.dispose()


if __name__=='__main__':main()
