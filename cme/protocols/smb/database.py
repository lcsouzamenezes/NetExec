#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
from sqlalchemy import MetaData, func, inspect, Table, select, insert, update, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.exc import IllegalStateChangeError
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SAWarning
from datetime import datetime
import asyncio
import warnings

# if there is an issue with SQLAlchemy and a connection cannot be cleaned up properly it spews out annoying warnings
warnings.filterwarnings("ignore", category=SAWarning)


def get_table_names(conn):
    inspector = inspect(conn)
    return inspector.get_table_names()


class database:
    def __init__(self, db_engine):
        self.ComputersTable = None
        self.UsersTable = None
        self.GroupsTable = None
        self.SharesTable = None
        self.AdminRelationsTable = None
        self.GroupRelationsTable = None
        self.LoggedinRelationsTable = None

        self.db_engine = db_engine
        self.metadata = MetaData()
        asyncio.run(self.reflect_tables())
        session_factory = sessionmaker(bind=self.db_engine, expire_on_commit=True, class_=AsyncSession)
        # session_factory = sessionmaker(bind=self.db_engine, expire_on_commit=False)
        Session = scoped_session(session_factory)
        # this is still named "conn" when it is the session object; TODO: rename
        self.conn = Session()

    async def shutdown_db(self):
        try:
            await asyncio.shield(self.conn.close())
        # due to the async nature of CME, sometimes session state is a bit messy and this will throw:
        # Method 'close()' can't be called here; method '_connection_for_bind()' is already in progress and
        # this would cause an unexpected state change to <SessionTransactionState.CLOSED: 5>
        except IllegalStateChangeError as e:
            logging.debug(f"Error while closing session db object: {e}")

    async def reflect_tables(self):
        async with self.db_engine.connect() as conn:
            await conn.run_sync(self.metadata.reflect)

            self.ComputersTable = Table("computers", self.metadata, autoload_with=self.db_engine)
            self.UsersTable = Table("users", self.metadata, autoload_with=self.db_engine)
            self.GroupsTable = Table("groups", self.metadata, autoload_with=self.db_engine)
            self.SharesTable = Table("shares", self.metadata, autoload_with=self.db_engine)
            self.AdminRelationsTable = Table("admin_relations", self.metadata, autoload_with=self.db_engine)
            self.GroupRelationsTable = Table("group_relations", self.metadata, autoload_with=self.db_engine)
            self.LoggedinRelationsTable = Table("loggedin_relations", self.metadata, autoload_with=self.db_engine)

    @staticmethod
    def db_schema(db_conn):
        db_conn.execute('''CREATE TABLE "computers" (
            "id" integer PRIMARY KEY ON CONFLICT REPLACE,
            "ip" text,
            "hostname" text,
            "domain" text,
            "os" text,
            "dc" boolean,
            "smbv1" boolean,
            "signing" boolean,
            "spooler" boolean,
            "zerologon" boolean,
            "petitpotam" boolean
            )''')

        # type = hash, plaintext
        db_conn.execute('''CREATE TABLE "users" (
            "id" integer PRIMARY KEY,
            "domain" text,
            "username" text,
            "password" text,
            "credtype" text,
            "pillaged_from_computerid" integer,
            FOREIGN KEY(pillaged_from_computerid) REFERENCES computers(id)
            )''')

        db_conn.execute('''CREATE TABLE "groups" (
            "id" integer PRIMARY KEY,
            "domain" text,
            "name" text,
            "member_count_ad" integer,
            "last_query_time" text
            )''')

        # This table keeps track of which credential has admin access over which machine and vice-versa
        db_conn.execute('''CREATE TABLE "admin_relations" (
            "id" integer PRIMARY KEY,
            "userid" integer,
            "computerid" integer,
            FOREIGN KEY(userid) REFERENCES users(id),
            FOREIGN KEY(computerid) REFERENCES computers(id)
            )''')

        db_conn.execute('''CREATE TABLE "loggedin_relations" (
            "id" integer PRIMARY KEY,
            "userid" integer,
            "computerid" integer,
            FOREIGN KEY(userid) REFERENCES users(id),
            FOREIGN KEY(computerid) REFERENCES computers(id)
            )''')

        db_conn.execute('''CREATE TABLE "group_relations" (
            "id" integer PRIMARY KEY,
            "userid" integer,
            "groupid" integer,
            FOREIGN KEY(userid) REFERENCES users(id),
            FOREIGN KEY(groupid) REFERENCES groups(id)
            )''')

        db_conn.execute('''CREATE TABLE "shares" (
            "id" integer PRIMARY KEY,
            "computerid" text,
            "userid" integer,
            "name" text,
            "remark" text,
            "read" boolean,
            "write" boolean,
            FOREIGN KEY(userid) REFERENCES users(id)
            UNIQUE(computerid, userid, name)
        )''')

        #db_conn.execute('''CREATE TABLE "ntds_dumps" (
        #    "id" integer PRIMARY KEY,
        #    "computerid", integer,
        #    "domain" text,
        #    "username" text,
        #    "hash" text,
        #    FOREIGN KEY(computerid) REFERENCES computers(id)
        #    )''')

    def add_share(self, computer_id, user_id, name, remark, read, write):
        share_data = {
            "computerid": computer_id,
            "userid": user_id,
            "name": name,
            "remark": remark,
            "read": read,
            "write": write,
        }
        share_id = asyncio.run(self.conn.execute(
            insert(self.SharesTable).values(share_data).returning(self.SharesTable.c.id)
        )).scalar_one()

        return share_id

    def is_share_valid(self, share_id):
        """
        Check if this share ID is valid.
        """
        q = select(self.SharesTable).filter(
            self.SharesTable.c.id == share_id
        )
        results = asyncio.run(self.conn.execute(q)).all()

        logging.debug(f"is_share_valid(shareID={share_id}) => {len(results) > 0}")
        return len(results) > 0

    def get_shares(self, filter_term=None):
        if self.is_share_valid(filter_term):
            q = select(self.SharesTable).filter(
                self.SharesTable.c.id == filter_term
            )
        elif filter_term:
            q = select(self.SharesTable).filter(
                func.lower(self.SharesTable.c.name).like(func.lower(f"%{filter_term}%"))
            )
        else:
            q = select(self.SharesTable)
        results = asyncio.run(self.conn.execute(q)).all()
        return results

    def get_shares_by_access(self, permissions, share_id=None):
        permissions = permissions.lower()
        q = select(self.SharesTable)
        if share_id:
            q.filter(self.SharesTable.c.id == share_id)
        if "r" in permissions:
            q.filter(self.SharesTable.c.read == 1)
        if "w" in permissions:
            q.filter(self.SharesTable.c.write == 1)
        results = asyncio.run(self.conn.execute(q)).all()
        return results

    def get_users_with_share_access(self, computer_id, share_name, permissions):
        permissions = permissions.lower()
        q = select(self.SharesTable.c.userid).filter(
            self.SharesTable.c.name == share_name,
            self.SharesTable.c.computerid == computer_id
        )
        if "r" in permissions:
            q.filter(self.SharesTable.c.read == 1)
        if "w" in permissions:
            q.filter(self.SharesTable.c.write == 1)
        results = asyncio.run(self.conn.execute(q)).all()

        return results

    # pull/545
    def add_computer(self, ip, hostname, domain, os, smbv1, signing, spooler=None, zerologon=None, petitpotam=None, dc=None):
        """
        Check if this host has already been added to the database, if not add it in.
        """
        domain = domain.split('.')[0].upper()
        update_hosts = []

        q = select(self.ComputersTable).filter(
            self.ComputersTable.c.ip == ip
        )
        results = asyncio.run(self.conn.execute(q)).all()
        logging.debug(f"Results in add_computer: {results}")

        host = {
            "ip": ip,
            "hostname": hostname,
            "domain": domain,
            "os": os,
            "dc": dc,
            "smbv1": smbv1,
            "signing": signing,
            "spooler": spooler,
            "zerologon": zerologon,
            "petitpotam": petitpotam
        }

        # create new computer
        if not results:
            update_hosts = [host]
        # update existing hosts data
        else:
            for host in results:
                computer_data = host._asdict()
                # only update column if it is being passed in
                if ip is not None:
                    computer_data["ip"] = ip
                if hostname is not None:
                    computer_data["hostname"] = hostname
                if domain is not None:
                    computer_data["domain"] = domain
                if os is not None:
                    computer_data["os"] = os
                if smbv1 is not None:
                    computer_data["smbv1"] = smbv1
                if signing is not None:
                    computer_data["signing"] = signing
                if spooler is not None:
                    computer_data["spooler"] = spooler
                if zerologon is not None:
                    computer_data["zerologon"] = zerologon
                if petitpotam is not None:
                    computer_data["petitpotam"] = petitpotam
                if dc is not None:
                    computer_data["dc"] = dc
                update_hosts.append(computer_data)

        logging.debug(f"Update Hosts: {update_hosts}")
        asyncio.run(
            self.conn.execute(
                sqlite_upsert(self.ComputersTable),
                update_hosts
            )
        )
        # asyncio.run(self.conn.close())

    def add_credential(self, credtype, domain, username, password, group_id=None, pillaged_from=None):
        """
        Check if this credential has already been added to the database, if not add it in.
        """
        domain = domain.split('.')[0].upper()
        user_rowid = None

        if group_id and not self.is_group_valid(group_id):
            self.conn.close()
            return

        if pillaged_from and not self.is_computer_valid(pillaged_from):
            self.conn.close()
            return

        credential_data = {}
        if credtype is not None:
            credential_data["credtype"] = credtype
        if domain is not None:
            credential_data["domain"] = domain
        if username is not None:
            credential_data["username"] = username
        if password is not None:
            credential_data["password"] = password
        if group_id is not None:
            credential_data["groupid"] = group_id
        if pillaged_from is not None:
            credential_data["pillaged_from"] = pillaged_from

        q = select(self.UsersTable).filter(
            func.lower(self.UsersTable.c.domain) == func.lower(domain),
            func.lower(self.UsersTable.c.username) == func.lower(username),
            func.lower(self.UsersTable.c.credtype) == func.lower(credtype)
        )
        results = asyncio.run(self.conn.execute(q)).all()

        logging.debug(f"Credential results: {results}")

        if not results:
            user_data = {
                "domain": domain,
                "username": username,
                "password": password,
                "credtype": credtype,
                "pillaged_from_computerid": pillaged_from,
            }
            # user_rowid = self.conn.execute(
            #     self.UsersTable.insert(),
            #     [user_data]
            # )
            q = insert(self.UsersTable).values(user_data)
            results = asyncio.run(self.conn.execute(q)).first()
            user_rowid = results[0]

            logging.debug(f"User RowID: {user_rowid}")
            if group_id:
                gr_data = {
                    "userid": user_rowid,
                    "groupid": group_id,
                }
                q = insert(self.GroupRelationsTable).values(gr_data)
                asyncio.run(self.conn.execute(q))
                # self.conn.execute(
                #     self.GroupRelationsTable.insert(),
                #     [gr_data]
                # )
        else:
            for user in results:
                # might be able to just remove this if check, but leaving it in for now
                if not user[3] and not user[4] and not user[5]:
                    # user_rowid = self.conn.execute(
                    #     self.UsersTable.update().values(
                    #         credential_data
                    #     ).where(
                    #         self.UsersTable.c.id == user[0]
                    #     )
                    # )
                    q = update(self.UsersTable).values(credential_data)
                    results = asyncio.run(self.conn.execute(q)).first()
                    user_rowid = results[0]

                    if group_id and not len(self.get_group_relations(user_rowid, group_id)):
                        gr_data = {
                            "userid": user_rowid,
                            "groupid": group_id,
                        }
                        q = update(self.GroupRelationsTable).values(gr_data)
                        asyncio.run(self.conn.execute(q))
                        # self.conn.execute(
                        #     self.GroupRelationsTable.update().values(
                        #         {"userid": user_rowid, "groupid": group_id}
                        #     )
                        # )
        logging.debug('add_credential(credtype={}, domain={}, username={}, password={}, groupid={}, pillaged_from={}) => {}'.format(
            credtype,
            domain,
            username,
            password,
            group_id,
            pillaged_from,
            user_rowid
        ))

        # try:
        #     self.conn.commit()
        # except Exception as e:
        #     logging.error(f"Exception while committing to database: {e}")
        #
        # self.conn.close()
        return user_rowid

    def add_user(self, domain, username, group_id=None):
        if group_id and not self.is_group_valid(group_id):
            return

        domain = domain.split('.')[0].upper()
        user_rowid = None

        user_data = {
            "password": "",
            "credtype": "",
            "pillaged_from_computerid": ""
        }
        if domain is not None:
            user_data["domain"] = domain
        if username is not None:
            user_data["username"] = username
        if group_id is not None:
            user_data["group_id"] = group_id

        results = self.conn.query(self.UsersTable).filter(
            func.lower(self.UsersTable.c.domain) == func.lower(domain),
            func.lower(self.UsersTable.c.username) == func.lower(username)
        ).all()

        if not len(results):
            user_rowid = self.conn.execute(
                self.UsersTable.insert(),
                [user_data]
            )
            if group_id:
                gr_data = {
                    "userid": user_rowid,
                    "groupid": group_id,
                }
                self.conn.execute(
                    self.GroupRelationsTable.insert(),
                    [gr_data]
                )
        else:
            for user in results:
                if (domain != user[1]) and (username != user[2]):
                    user_rowid = self.conn.execute(self.UsersTable.update().values(
                        user_data
                    ).where(
                        self.UsersTable.c.id == user[0]
                    ))
                if not user_rowid: user_rowid = user[0]
                if group_id and not len(self.get_group_relations(user_rowid, group_id)):
                    self.conn.execute(self.GroupRelationsTable.update().values(
                        {"userid": user_rowid, "groupid": group_id}
                    ))
        self.conn.commit()
        self.conn.close()

        logging.debug('add_user(domain={}, username={}, groupid={}) => {}'.format(domain, username, group_id, user_rowid))

        return user_rowid

    def add_group(self, domain, name, member_count_ad=None):
        domain = domain.split('.')[0].upper()

        results = self.conn.query(self.GroupsTable).filter(
            func.lower(self.GroupsTable.c.domain) == func.lower(domain),
            func.lower(self.GroupsTable.c.name) == func.lower(name)
        ).all()

        group_data = {}
        if domain is not None:
            group_data["domain"] = domain
        if name is not None:
            group_data["name"] = name
        if member_count_ad is not None:
            group_data["member_count_ad"] = member_count_ad
            today = datetime.now()
            iso_date = today.isoformat()
            group_data["last_query_time"] = iso_date

        if results:
            # initialize the cid to the first (or only) id
            cid = results[0][0]
            for group in results:
                cid = self.conn.execute(
                    self.GroupsTable.update().values(
                        group_data
                    ).where(
                        self.GroupsTable.c.id == group[0]
                    )
                )
        else:
            cid = self.conn.execute(
                self.GroupsTable.insert(),
                [group_data]
            )
        self.conn.commit()
        self.conn.close()

        logging.debug('add_group(domain={}, name={}) => {}'.format(domain, name, cid))
        return cid

    def remove_credentials(self, creds_id):
        """
        Removes a credential ID from the database
        """
        for cred_id in creds_id:
            self.conn.query(self.UsersTable).filter(
                self.UsersTable.c.id == cred_id
            ).delete()
        self.conn.commit()
        self.conn.close()

    def add_admin_user(self, credtype, domain, username, password, host, user_id=None):
        domain = domain.split('.')[0].upper()

        if user_id:
            q = select(self.UsersTable).filter(
                self.UsersTable.c.id == user_id
            )
            users = asyncio.run(self.conn.execute(q)).all()
        else:
            q = select(self.UsersTable).filter(
                self.UsersTable.c.credtype == credtype,
                func.lower(self.UsersTable.c.domain) == func.lower(domain),
                func.lower(self.UsersTable.c.username) == func.lower(username),
                self.UsersTable.c.password == password
            )
            users = asyncio.run(self.conn.execute(q)).all()
        logging.debug(f"Users: {users}")

        q = select(self.ComputersTable).filter(
            self.ComputersTable.c.ip.like(func.lower(f"%{host}%"))
        )
        hosts = asyncio.run(self.conn.execute(q)).all()
        logging.debug(f"Hosts: {hosts}")

        if users is not None and hosts is not None:
            for user, host in zip(users, hosts):
                user_id = user[0]
                host_id = host[0]

                # Check to see if we already added this link
                # links = self.conn.query(self.AdminRelationsTable).filter(
                #     self.AdminRelationsTable.c.userid == user_id,
                #     self.AdminRelationsTable.c.computerid == host_id
                # ).all()
                q = select(self.AdminRelationsTable).filter(
                    self.AdminRelationsTable.c.userid == user_id,
                    self.AdminRelationsTable.c.computerid == host_id
                )
                links = asyncio.run(self.conn.execute(q)).all()

                if not links:
                    link = {"userid": user_id, "computerid": host_id}
                    # self.conn.execute(
                    #     self.AdminRelationsTable.insert(),
                    #     [{"userid": user_id, "computerid": host_id}]
                    # )
                    asyncio.run(self.conn.execute(
                        insert(self.AdminRelationsTable).values(link)
                    ))

    def get_admin_relations(self, user_id=None, host_id=None):
        if user_id:
            q = select(self.AdminRelationsTable).filter(
                self.AdminRelationsTable.c.userid == user_id
            )
        elif host_id:
            q = select(self.AdminRelationsTable).filter(
                self.AdminRelationsTable.c.computerid == host_id
            )
        else:
            q = select(self.AdminRelationsTable)

        results = asyncio.run(self.conn.execute(q)).all()
        return results

    def get_group_relations(self, user_id=None, group_id=None):
        if user_id and group_id:
            q = select(self.GroupRelationsTable).filter(
                self.GroupRelationsTable.c.id == user_id,
                self.GroupRelationsTable.c.groupid == group_id
            )
        elif user_id:
            q = select(self.GroupRelationsTable).filter(
                self.GroupRelationsTable.c.id == user_id
            )
        elif group_id:
            q = select(self.GroupRelationsTable).filter(
                self.GroupRelationsTable.c.groupid == group_id
            )

        results = asyncio.run(self.conn.execute(q)).all()
        return results

    def remove_admin_relation(self, user_ids=None, host_ids=None):
        if user_ids:
            for user_id in user_ids:
                self.conn.query(self.AdminRelationsTable).filter(
                    self.AdminRelationsTable.c.userid == user_id
                ).delete()
        elif host_ids:
            for host_id in host_ids:
                self.conn.query(self.AdminRelationsTable).filter(
                    self.AdminRelationsTable.c.hostid == host_id
                ).delete()
        self.conn.commit()
        self.conn.close()

    def remove_group_relations(self, user_id=None, group_id=None):
        if user_id:
            self.conn.query(self.GroupRelationsTable).filter(
                self.GroupRelationsTable.c.userid == user_id
            ).delete()
        elif group_id:
            self.conn.query(self.GroupRelationsTable).filter(
                self.GroupRelationsTable.c.groupid == group_id
            ).delete()
        self.conn.commit()
        self.conn.close()

    def is_credential_valid(self, credential_id):
        """
        Check if this credential ID is valid.
        """
        q = select(self.UsersTable).filter(
            self.UsersTable.c.id == credential_id,
            self.UsersTable.c.password is not None
        )
        results = asyncio.run(self.conn.execute(q)).all()
        return len(results) > 0

    def is_credential_local(self, credential_id):
        user_domain = self.conn.query(self.UsersTable.c.domain).filter(
            self.UsersTable.c.id == credential_id
        ).all()

        if user_domain:
            results = self.conn.query(self.ComputersTable).filter(
                func.lower(self.ComputersTable.c.id) == func.lower(user_domain)
            ).all()
            self.conn.commit()
            self.conn.close()
            return len(results) > 0

    def get_credentials(self, filter_term=None, cred_type=None):
        """
        Return credentials from the database.
        """
        # if we're returning a single credential by ID
        if self.is_credential_valid(filter_term):
            q = select(self.UsersTable).filter(
                self.UsersTable.c.id == filter_term
            )
        elif cred_type:
            q = select(self.UsersTable).filter(
                self.UsersTable.c.credtype == cred_type
            )
        # if we're filtering by username
        elif filter_term and filter_term != '':
            q = select(self.UsersTable).filter(
                func.lower(self.UsersTable.c.username).like(func.lower(f"%{filter_term}%"))
            )
        # otherwise return all credentials
        else:
            q = select(self.UsersTable)

        results = asyncio.run(self.conn.execute(q)).all()
        return results

    def is_user_valid(self, user_id):
        """
        Check if this User ID is valid.
        """
        q = select(self.UsersTable).filter(
            self.UsersTable.c.id == user_id
        )
        results = asyncio.run(self.conn.execute(q)).all()
        return len(results) > 0

    def get_users(self, filter_term=None):
        if self.is_user_valid(filter_term):
            results = self.conn.query(self.UsersTable).filter(
                self.UsersTable.c.id == filter_term
            ).all()
        # if we're filtering by username
        elif filter_term and filter_term != '':
            results = self.conn.query(self.UsersTable).filter(
                func.lower(self.UsersTable.c.username).like(func.lower(f"%{filter_term}%"))
            ).all()
        else:
            results = self.conn.query(self.UsersTable).all()

        self.conn.commit()
        self.conn.close()
        return results

    def get_user(self, domain, username):
        results = self.conn.query(self.UsersTable).filter(
            func.lower(self.UsersTable.c.domain) == func.lower(domain),
            func.lower(self.UsersTable.c.username) == func.lower(username)
        ).all()
        self.conn.commit()
        self.conn.close()
        return results

    def is_computer_valid(self, host_id):
        """
        Check if this host ID is valid.
        """
        results = self.conn.query(self.ComputersTable).filter(
            self.ComputersTable.c.id == host_id
        ).all()
        self.conn.commit()
        self.conn.close()
        return len(results) > 0

    def get_computers(self, filter_term=None, domain=None):
        """
        Return hosts from the database.
        """
        print(f"HERE!")
        # if we're returning a single host by ID
        if self.is_computer_valid(filter_term):
            results = self.conn.query(self.ComputersTable).filter(
                self.ComputersTable.c.id == filter_term
            ).first()
        # if we're filtering by domain controllers
        elif filter_term == 'dc':
            if domain:
                results = self.conn.query(self.ComputersTable).filter(
                    self.ComputersTable.c.dc == 1,
                    func.lower(self.ComputersTable.c.domain) == func.lower(domain)
                ).all()
            else:
                results = self.conn.query(self.ComputersTable).filter(
                    self.ComputersTable.c.dc == 1
                ).all()
        # if we're filtering by ip/hostname
        elif filter_term and filter_term != "":
            results = self.conn.query(self.ComputersTable).filter((
                    func.lower(self.ComputersTable.c.ip).like(func.lower(f"%{filter_term}%")) |
                    func.lower(self.ComputersTable.c.hostname).like(func.lower(f"%{filter_term}"))
            )).all()
        # otherwise return all computers
        else:
            results = self.conn.query(self.ComputersTable).all()

        self.conn.commit()
        self.conn.close()
        return results

    def get_domain_controllers(self, domain=None):
        return self.get_computers(filter_term='dc', domain=domain)

    def is_group_valid(self, group_id):
        """
        Check if this group ID is valid.
        """
        q = select(self.GroupsTable).filter(
            self.GroupsTable.c.id == group_id
        )
        results = asyncio.run(self.conn.execute(q)).first()

        valid = True if results else False
        logging.debug(f"is_group_valid(groupID={group_id}) => {valid}")

        return valid

    def get_groups(self, filter_term=None, group_name=None, group_domain=None):
        """
        Return groups from the database
        """
        if group_domain:
            group_domain = group_domain.split('.')[0].upper()

        if self.is_group_valid(filter_term):
            results = self.conn.query(self.GroupsTable).filter(
                self.GroupsTable.c.id == filter_term
            ).first()
        elif group_name and group_domain:
            results = self.conn.query(self.GroupsTable).filter(
                func.lower(self.GroupsTable.c.username) == func.lower(group_name),
                func.lower(self.GroupsTable.c.domain) == func.lower(group_domain)
            ).all()
        elif filter_term and filter_term != "":
            results = self.conn.query(self.GroupsTable).filter(
                func.lower(self.GroupsTable.c.name).like(func.lower(f"%{filter_term}%"))
            ).all()
        else:
            results = self.conn.query(self.GroupsTable).all()

        self.conn.commit()
        self.conn.close()
        logging.debug(f"get_groups(filterTerm={filter_term}, groupName={group_name}, groupDomain={group_domain}) => {results}")
        return results

    def clear_database(self):
        for table in self.metadata.tables:
            self.conn.query(self.metadata.tables[table]).delete()
        self.conn.commit()
