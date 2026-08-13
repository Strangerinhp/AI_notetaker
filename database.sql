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

SELECT
    Id, Title, FileName, Engine, Status,
    CreatedAt, LastEditedAt, UpdatedAt
FROM dbo.MeetingHistory
ORDER BY UpdatedAt DESC;
GO
