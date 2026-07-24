"""Generate a realistic mini artifact library for end-to-end testing.

Built programmatically because fixed-format COBOL is column-exact: the whole
point of several fixtures is that content in columns 1-6 and 73-80 must NOT be
scanned, and hand-aligning that in a heredoc is how you get a test that passes
for the wrong reason.
"""

from __future__ import annotations

import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "fixtures")


def cob(lines: list[str], ident: str = "") -> str:
    """Lay COBOL out in real fixed format: seq(1-6) ind(7) code(8-72) id(73-80)."""
    out = []
    for i, (ind, code) in enumerate(lines, start=1):
        seq = f"{i * 100:06d}"
        body = f"{seq}{ind}{code:<65}"[:72]
        out.append(f"{body:<72}{ident[:8]:<8}".rstrip())
    return "\n".join(out) + "\n"


def write(rel: str, text: str) -> None:
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)


def build() -> None:
    if os.path.isdir(ROOT):
        # ignore_errors: OneDrive keeps a handle on directories it is syncing,
        # which makes a plain rmtree fail with WinError 5 on this machine.
        shutil.rmtree(ROOT, ignore_errors=True)

    # ---------------------------------------------------------- DCLGEN
    write("PROD.DCLGEN/DCLCUST", cob([
        (" ", "     EXEC SQL DECLARE PRODDB.CUSTOMER TABLE"),
        (" ", "     ( CUST_ID              CHAR(10) NOT NULL,"),
        (" ", "       CUST_NAME            VARCHAR(40),"),
        (" ", "       BALANCE              DECIMAL(11,2)"),
        (" ", "     ) END-EXEC."),
        (" ", " 01  DCLCUSTOMER."),
        (" ", "     10 CUST-ID            PIC X(10)."),
        (" ", "     10 CUST-NAME."),
        (" ", "        49 CUST-NAME-LEN    PIC S9(4) COMP."),
        (" ", "        49 CUST-NAME-TEXT   PIC X(40)."),
        (" ", "     10 BALANCE            PIC S9(9)V99 COMP-3."),
    ], ident="DCLCUST"))

    # ---------------------------------------------------------- COBOL: main
    # Exercises every trap at once: a comment naming a phantom table, the
    # ident field in cols 73-80 spelling CUSTOMER, a table name broken across
    # a column-72 continuation, and a literal containing SQL text.
    write("PROD.COBOL.SRC/CUSTUPD", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTUPD."),
        (" ", "DATA DIVISION."),
        (" ", "WORKING-STORAGE SECTION."),
        ("*", "  THIS COMMENT MENTIONS FROM PRODDB.GHOST_TABLE ON PURPOSE"),
        (" ", "     COPY DCLCUST."),
        (" ", " 01  WS-MSG   PIC X(40) VALUE 'SELECT FROM PRODDB.NOTREAL'."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     EXEC SQL"),
        (" ", "        SELECT CUST_NAME, BALANCE"),
        (" ", "          INTO :CUST-NAME, :BALANCE"),
        (" ", "          FROM PRODDB.CUSTOMER"),
        (" ", "         WHERE CUST_ID = :CUST-ID"),
        (" ", "     END-EXEC."),
        (" ", "     EXEC SQL"),
        (" ", "        UPDATE PRODDB.CUSTOMER"),
        (" ", "           SET BALANCE = :BALANCE"),
        (" ", "         WHERE CUST_ID = :CUST-ID"),
        (" ", "     END-EXEC."),
        (" ", "     EXEC SQL"),
        (" ", "        INSERT INTO PRODDB.CUST_AUDIT"),
        (" ", "        SELECT * FROM PRODDB.CUSTOMER_HIST"),
        (" ", "     END-EXEC."),
        (" ", "     CALL 'CUSTVAL' USING DCLCUSTOMER."),
        (" ", "     STOP RUN."),
    ], ident="CUSTOMER"))          # <- ident field must never be scanned

    # ---------------------------------------------------------- COBOL: caller
    write("PROD.COBOL.SRC/CUSTDRV", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTDRV."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     CALL 'CUSTUPD'."),
        (" ", "     STOP RUN."),
    ], ident="CUSTDRV"))

    # ---------------------------------------------------------- COBOL: copybook user
    write("PROD.COBOL.SRC/CUSTRPT", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTRPT."),
        (" ", "DATA DIVISION."),
        (" ", "WORKING-STORAGE SECTION."),
        (" ", "     COPY DCLCUST."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     STOP RUN."),
    ], ident="CUSTRPT"))

    # ---------------------------------------------------------- COBOL: dynamic SQL
    write("PROD.COBOL.SRC/CUSTDYN", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTDYN."),
        (" ", "DATA DIVISION."),
        (" ", "WORKING-STORAGE SECTION."),
        (" ", " 01  WS-SQL  PIC X(80)."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     MOVE 'DELETE FROM PRODDB.CUSTOMER WHERE CUST_ID = ?'"),
        (" ", "       TO WS-SQL."),
        (" ", "     EXEC SQL PREPARE S1 FROM :WS-SQL END-EXEC."),
        (" ", "     EXEC SQL EXECUTE S1 END-EXEC."),
        (" ", "     STOP RUN."),
    ], ident="CUSTDYN"))

    # ---------------------------------------------------------- COBOL: continuation
    # PRODDB.CUS / TOMER split at column 72 - invisible to any grep.
    write("PROD.COBOL.SRC/CUSTCON", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTCON."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     EXEC SQL DELETE FROM PRODDB.CUS"),
        ("-", "TOMER WHERE CUST_ID = :X END-EXEC."),
        (" ", "     STOP RUN."),
    ], ident="CUSTCON"))

    # ---------------------------------------------------------- PROC
    write("PROD.PROCLIB/CUSTPRC", """\
//CUSTPRC  PROC DB2SYS=DB2P,PLANNM=CUSTPLAN
//RUNSTEP  EXEC PGM=IKJEFT01,DYNAMNBR=20,COND=(4,LT)
//STEPLIB  DD DSN=PROD.DB2.LOADLIB,DISP=SHR
//SYSTSPRT DD SYSOUT=*
//SYSTSIN  DD *
  DSN SYSTEM(&DB2SYS)
  RUN PROGRAM(CUSTUPD) PLAN(&PLANNM) LIB('PROD.DB2.LOADLIB')
  END
/*
//         PEND
""")

    # ---------------------------------------------------------- JCL: calls PROC
    write("PROD.JCL.CNTL/CUSTJOB1", """\
//CUSTJOB1 JOB (ACCT),'NIGHTLY CUSTOMER',CLASS=A,MSGCLASS=X
//*  THIS COMMENT SAYS UPDATE PRODDB.GHOST_TABLE AND MUST BE IGNORED
//JOBLIB   DD DSN=PROD.DB2.LOADLIB,DISP=SHR
//         JCLLIB ORDER=(PROD.PROCLIB)
//STEP010  EXEC CUSTPRC,DB2SYS=DB2P
//STEP020  EXEC PGM=IEFBR14
//DUMMY    DD DUMMY
""")

    # ---------------------------------------------------------- JCL: utility
    write("PROD.JCL.CNTL/CUSTLOAD", """\
//CUSTLOAD JOB (ACCT),'RELOAD CUSTOMER',CLASS=A
//LOADSTEP EXEC PGM=DSNUTILB,REGION=0M,PARM='DB2P,LOADCUST'
//STEPLIB  DD DSN=DSN.SDSNLOAD,DISP=SHR
//SYSREC   DD DSN=PROD.CUST.EXTRACT,DISP=SHR
//SYSIN    DD *
  LOAD DATA INDDN SYSREC LOG NO REPLACE
       INTO TABLE PRODDB.CUSTOMER
       ( CUST_ID   POSITION(1)  CHAR(10)
       , CUST_NAME POSITION(11) VARCHAR
       )
/*
//REORGSTP EXEC PGM=DSNUTILB,PARM='DB2P,REORGCUS'
//SYSIN    DD *
  REORG TABLESPACE PRODDB01.TSCUST SHRLEVEL REFERENCE
/*
//SQLSTEP  EXEC PGM=IKJEFT01
//SYSTSIN  DD *
  DSN SYSTEM(DB2P)
  RUN PROGRAM(DSNTEP2) PLAN(DSNTEP2)
  END
/*
//SYSIN    DD *
  SELECT COUNT(*) FROM PRODDB.CUSTOMER;
/*
""")

    # ---------------------------------------------------------- JCL: sort deck
    # Instream data under an unknown program - must NOT invent dependencies.
    write("PROD.JCL.CNTL/SORTJOB", """\
//SORTJOB  JOB (ACCT),'SORT ONLY',CLASS=A
//SORTSTEP EXEC PGM=SORT
//SORTIN   DD DSN=PROD.CUST.EXTRACT,DISP=SHR
//SYSIN    DD *
  SORT FIELDS=(1,10,CH,A)
  INCLUDE COND=(11,3,CH,EQ,C'ABC')
  OUTFIL FNAMES=OUT1,INCLUDE=(1,10,CH,NE,C' ')
/*
""")

    # ---------------------------------------------------------- cataloged decks
    # Most shops catalog their utility decks rather than inlining them, so the
    # JCL only ever names a DSN. Without resolving that reference the LOAD card
    # is found but the job that runs it is not.
    write("PROD.CNTL/LOADCUST", """\
  LOAD DATA INDDN SYSREC LOG NO REPLACE
       INTO TABLE PRODDB.CUSTOMER
       ( CUST_ID   POSITION(1)  CHAR(10)
       , CUST_NAME POSITION(11) VARCHAR
       )
""")

    write("PROD.CNTL/UNLDCUST", """\
  UNLOAD TABLESPACE PRODDB01.TSCUST
         FROM TABLE PRODDB.CUSTOMER
         ( CUST_ID, CUST_NAME, BALANCE )
""")

    # A sort deck names no table at all, but it is the middle of the
    # UNLOAD -> sort -> LOAD chain and must still be identified.
    write("PROD.CNTL/SORTCUST", """\
  SORT FIELDS=(1,10,CH,A)
  INCLUDE COND=(11,3,CH,EQ,C'ABC')
  OUTFIL FNAMES=OUT1,INCLUDE=(1,10,CH,NE,C' ')
""")

    # Same member name, different library, same table. The JCL below points at
    # PROD.CNTL - this one must NOT be wired to it.
    write("TEST.CNTL/LOADCUST", """\
  LOAD DATA INDDN SYSREC LOG NO RESUME YES
       INTO TABLE PRODDB.CUSTOMER
""")

    write("PROD.JCL.CNTL/CUSTEXT", """\
//CUSTEXT  JOB (ACCT),'EXTRACT AND RELOAD',CLASS=A
//UNLDSTEP EXEC PGM=DSNUTILB,PARM='DB2P,UNLDCUST'
//SYSREC   DD DSN=PROD.CUST.EXTRACT(+1),DISP=(NEW,CATLG)
//SYSIN    DD DSN=PROD.CNTL(UNLDCUST),DISP=SHR
//SORTSTEP EXEC PGM=SORT
//SORTIN   DD DSN=PROD.CUST.EXTRACT(0),DISP=SHR
//SORTOUT  DD DSN=PROD.CUST.SORTED,DISP=(NEW,CATLG)
//SYSIN    DD DSN=PROD.CNTL(SORTCUST),DISP=SHR
//LOADSTEP EXEC PGM=DSNUTILB,PARM='DB2P,LOADCUST'
//SYSREC   DD DSN=PROD.CUST.SORTED,DISP=SHR
//SYSIN    DD DSN=PROD.CNTL(LOADCUST),DISP=SHR
""")

    # A PROC that points at a cataloged deck, invoked by a job below.
    write("PROD.PROCLIB/CUSTLPRC", """\
//CUSTLPRC PROC
//RELOAD   EXEC PGM=DSNUTILB,PARM='DB2P,LOADCUST'
//SYSIN    DD DSN=PROD.CNTL(LOADCUST),DISP=SHR
//         PEND
""")

    write("PROD.JCL.CNTL/CUSTJOB2", """\
//CUSTJOB2 JOB (ACCT),'RELOAD VIA PROC',CLASS=A
//         JCLLIB ORDER=(PROD.PROCLIB)
//STEP010  EXEC CUSTLPRC
""")

    # ---------------------------------------------------------- SQL: DDL + view
    write("PROD.SQL.DDL/CUSTDDL.sql", """\
-- Customer table definition
CREATE TABLE PRODDB.CUSTOMER
  ( CUST_ID    CHAR(10)     NOT NULL
  , CUST_NAME  VARCHAR(40)
  , BALANCE    DECIMAL(11,2)
  , PRIMARY KEY (CUST_ID)
  ) IN PRODDB01.TSCUST;

CREATE INDEX PRODDB.IXCUST01 ON PRODDB.CUSTOMER (CUST_ID);

CREATE VIEW PRODDB.V_ACTIVE_CUST AS
  SELECT CUST_ID, CUST_NAME FROM PRODDB.CUSTOMER WHERE BALANCE > 0;

CREATE SYNONYM CUSTSYN FOR PRODDB.CUSTOMER;

CREATE TABLE PRODDB.CUST_AUDIT
  ( AUDIT_ID   INTEGER NOT NULL
  , CUST_ID    CHAR(10) NOT NULL
  , CONSTRAINT FK_AUD FOREIGN KEY (CUST_ID) REFERENCES PRODDB.CUSTOMER
  ) IN PRODDB01.TSAUD;
""")

    # ---------------------------------------------------------- SQL: stored proc
    write("PROD.SQL.SPROC/SPCUSTUP.sql", """\
CREATE PROCEDURE PRODDB.SP_CUST_UPDATE (IN P_ID CHAR(10), IN P_BAL DECIMAL(11,2))
  LANGUAGE SQL
BEGIN
  DECLARE V_CNT INTEGER;
  -- this comment mentions PRODDB.GHOST_TABLE and must be ignored
  SELECT COUNT(*) INTO V_CNT FROM PRODDB.CUSTOMER WHERE CUST_ID = P_ID;
  IF V_CNT > 0 THEN
     UPDATE PRODDB.CUSTOMER SET BALANCE = P_BAL WHERE CUST_ID = P_ID;
  ELSE
     INSERT INTO PRODDB.CUSTOMER (CUST_ID, BALANCE) VALUES (P_ID, P_BAL);
  END IF;
  MERGE INTO PRODDB.CUST_AUDIT AS A
    USING (SELECT P_ID AS ID FROM SYSIBM.SYSDUMMY1) AS S
    ON A.CUST_ID = S.ID
    WHEN NOT MATCHED THEN INSERT (CUST_ID) VALUES (S.ID);
END
""")

    # ---------------------------------------------------------- view consumer
    write("PROD.COBOL.SRC/CUSTVW", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. CUSTVW."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     EXEC SQL"),
        (" ", "        SELECT CUST_NAME INTO :N"),
        (" ", "          FROM PRODDB.V_ACTIVE_CUST"),
        (" ", "     END-EXEC."),
        (" ", "     STOP RUN."),
    ], ident="CUSTVW"))

    # ---------------------------------------------------------- unrelated
    write("PROD.COBOL.SRC/ORDPROC", cob([
        (" ", "IDENTIFICATION DIVISION."),
        (" ", "PROGRAM-ID. ORDPROC."),
        (" ", "PROCEDURE DIVISION."),
        (" ", "     EXEC SQL"),
        (" ", "        SELECT * FROM PRODDB.ORDERS"),
        (" ", "     END-EXEC."),
        (" ", "     STOP RUN."),
    ], ident="ORDPROC"))

    print(f"fixtures written to {ROOT}")


if __name__ == "__main__":
    build()
