IF DB_ID(N'MeetNote') IS NULL
BEGIN
    CREATE DATABASE [MeetNote];
END;
GO

USE [MeetNote];
GO

IF OBJECT_ID(N'dbo.MeetingHistory', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.MeetingHistory
    (
        Id UNIQUEIDENTIFIER NOT NULL
            CONSTRAINT PK_MeetingHistory PRIMARY KEY,
        Title NVARCHAR(180) NOT NULL,
        FileName NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_MeetingHistory_FileName DEFAULT N'',
        Engine NVARCHAR(32) NOT NULL
            CONSTRAINT DF_MeetingHistory_Engine DEFAULT N'',
        Transcript NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_MeetingHistory_Transcript DEFAULT N'',
        Minutes NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_MeetingHistory_Minutes DEFAULT N'',
        WordFilePath NVARCHAR(1024) NULL,
        WordFileName NVARCHAR(260) NULL,
        WordUpdatedAt DATETIME2(3) NULL,
        DiarizationSegments NVARCHAR(MAX) NOT NULL
            CONSTRAINT DF_MeetingHistory_DiarizationSegments DEFAULT N'[]',
        Status NVARCHAR(32) NOT NULL
            CONSTRAINT DF_MeetingHistory_Status DEFAULT N'queued',
        StatusMessage NVARCHAR(500) NOT NULL
            CONSTRAINT DF_MeetingHistory_StatusMessage DEFAULT N'',
        FileCount INT NOT NULL
            CONSTRAINT DF_MeetingHistory_FileCount DEFAULT 1,
        TotalAudioBytes BIGINT NOT NULL
            CONSTRAINT DF_MeetingHistory_TotalAudioBytes DEFAULT 0,
        CreatedAt DATETIME2(3) NOT NULL
            CONSTRAINT DF_MeetingHistory_CreatedAt DEFAULT SYSUTCDATETIME(),
        CompletedAt DATETIME2(3) NULL,
        LastEditedAt DATETIME2(3) NULL,
        UpdatedAt DATETIME2(3) NOT NULL
            CONSTRAINT DF_MeetingHistory_UpdatedAt DEFAULT SYSUTCDATETIME()
    );

    CREATE INDEX IX_MeetingHistory_Status_UpdatedAt
        ON dbo.MeetingHistory (Status, UpdatedAt DESC);
END;
GO

-- Safe, idempotent migration for databases created by older MeetNote versions.
-- An existing WordDocument column is deliberately left untouched so this script
-- never destroys legacy data. Current app versions no longer read or write it.
IF COL_LENGTH(N'dbo.MeetingHistory', N'WordFilePath') IS NULL
BEGIN
    ALTER TABLE dbo.MeetingHistory ADD WordFilePath NVARCHAR(1024) NULL;
END;
GO

IF COL_LENGTH(N'dbo.MeetingHistory', N'WordFileName') IS NULL
BEGIN
    ALTER TABLE dbo.MeetingHistory ADD WordFileName NVARCHAR(260) NULL;
END;
GO

IF COL_LENGTH(N'dbo.MeetingHistory', N'WordUpdatedAt') IS NULL
BEGIN
    ALTER TABLE dbo.MeetingHistory ADD WordUpdatedAt DATETIME2(3) NULL;
END;
GO

SELECT
    Id, Title, FileName, Engine, Status, WordFilePath, WordFileName, WordUpdatedAt,
    CreatedAt, LastEditedAt, UpdatedAt
FROM dbo.MeetingHistory
ORDER BY UpdatedAt DESC;
GO
